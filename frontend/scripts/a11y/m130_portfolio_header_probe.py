"""M130 — the portfolio header: value, day change, range chart — LIVE.

    python3 m130_portfolio_header_probe.py [--base https://fintrac.princessandmark.com]
                                           [--width 390] [--height 844]

AGENTS §4 step 5. Two tenants, because the two halves need different things:

A. A THROWAWAY tenant — the only place numbers can be pinned by hand:

  1  seed    register → TFSA + RRSP + one CHEQUING control carrying 999,999.00
             of committed cash (it must never reach the portfolio) → two CAD
             securities, contributions that leave BOTH accounts at exactly
             0.00 cash, and manual closes at `today-4` and `today` with
             NOTHING between them — the Boxing-Day shape, so the baseline has
             to be carried forward to `today-4`:

                 AAA 100 sh  90 → 100     BBB 200 sh  18 → 20
                 today   100×100 + 200×20 = 14,000.00
                 prior   100×90  + 200×18 = 12,600.00
                 day Δ = 1,400.00  (+11.11% of 12,600.00)

  2  wire    the ANTI-FABRICATION identity, read off the wire: Σ of
             `market_value_cad` == `total_value_cad` == `portfolio_value_cad`
             (cash is 0.00), `total_day_change_cad=1400.00`,
             `total_day_change_pct=11.11`, `day_change_as_of=today-4`,
             `day_change_mixed_baseline=false`, and the value-history
             `stats.change_cad == last bucket − first bucket` (the chart's own
             first and last point).
  3  header  the card renders exactly those numbers: $14,000.00 ·
             ▲ +$1,400.00 (+11.11%) · "vs <Mon D> close" · no "live" claim,
             and NO cash line (zero cash needs nothing to explain).
  4  sigma   the positions Σ row prints the SAME $14,000.00 and the SAME
             +11.11% — header text == Σ-row text, asserted in the DOM.
  5  tick    a slow-network hold on the positions refetch, then POST /prices
             (100 → 110): the header re-evaluates LIVE to $15,000.00 and
             ▲ +$2,400.00 (+19.05%) with a "live" badge — every figure
             hand-checkable — and cash still does not move. Disarm → the
             server payload replaces it and the badge disappears.
  6  range   the `6M` button is a real toggle (`aria-pressed`), the sentence
             relabels to "Over 6 months:", and the request goes out with a
             `from`/`to` window.
  7  a11y    axe 0 critical + 0 serious on /investments, both themes; the
             range group and the chart's `<details>` fallback are reachable by
             keyboard; the header does not clip at the probe's width (the
             card, the value and the Δ all stay inside the viewport).
  8  console no errors.

B. The DEMO tenant — the only place a REAL portfolio already exists:

  9  demo    the header's value equals the Σ of the served positions rows
             (computed in-page from the SAME payload the table renders), the
             day change carries a real "vs <date> close" caption, the Σ row's
             CAD total matches the header, the chart draws (has_data with
             buckets), and switching `1M` relabels the sentence to the window
             the server actually answered with — one range state, one number.

Exit 0 only if every check passes; JSON + PNG evidence land next to this
script.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from a11y_driver import USER_DATA_DIR, Browser  # noqa: E402
from axe_scan import load_axe, run_axe, set_theme, summarize  # noqa: E402
from probe_target import random_password

CHECKS: list[dict] = []

PASSWORD = random_password("m130")
DEMO_EMAIL = "demo@fintracker.local"
DEMO_PASSWORD = "demo-password"  # the public demo door's credential (docs/30)
SYMBOL_A = "M130AAA"
SYMBOL_B = "M130BBB"


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


def close_label(day_iso: str) -> str:
    """`2026-10-05` → `Oct 5`, the browser's own calendar (same parse as the UI)."""
    y, m, d = (int(part) for part in day_iso.split("-"))
    return date(y, m, d).strftime("%b %-d")


# --- tenant A: seed ---------------------------------------------------------


def seed(tab, stamp: int) -> dict:
    today = datetime.now(UTC).date()
    prior = (today - timedelta(days=4)).isoformat()
    email = f"m130-probe-{stamp}@example.com"
    return tab.evaluate(
        f"""(async () => {{
      const email = {json.dumps(email)}, password = {json.dumps(PASSWORD)};
      const h = {{'Content-Type': 'application/json'}};
      await fetch('/api/v1/auth/register',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const log = await fetch('/api/v1/auth/login',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const auth = {{...h, Authorization: 'Bearer ' + (await log.json()).access_token}};
      const logSteps = [];
      const post = async (path, body) => {{
        const r = await fetch('/api/v1' + path, {{method:'POST', headers:auth,
                                                  body: JSON.stringify(body)}});
        const text = await r.text();
        let data;
        try {{ data = JSON.parse(text); }} catch (e) {{ data = {{detail: text.slice(0,160)}}; }}
        logSteps.push({{path, status: r.status, id: data && data.id,
                        detail: data && data.detail, code: data && data.code}});
        return data;
      }};

      const tfsa = (await post('/accounts',
        {{name:'M130 TFSA', type:'tfsa', currency:'CAD'}})).id;
      const rrsp = (await post('/accounts',
        {{name:'M130 RRSP', type:'rrsp', currency:'CAD'}})).id;
      // The scope control: committed CASH outside the investment accounts.
      const chequing = (await post('/accounts',
        {{name:'M130 Chequing', type:'chequing', currency:'CAD'}})).id;
      await post('/transactions', {{account_id: chequing,
        date: new Date(Date.now() - 40*86400000).toISOString().slice(0,10),
        raw_description: 'Payroll', normalized_payee: 'Payroll',
        amount_native: '999999.00', currency: 'CAD'}});

      // `mk` must hand back the ID, not the whole create-response: every
      // later write interpolates it into a request body, where an object
      // serialises as a nested object and the API answers 422.
      const mk = async (symbol) => (await post('/securities',
        {{symbol, name: symbol + ' co', kind:'stock',
          listing_currency:'CAD', exchange:'TSX'}})).id;
      const a = await mk({json.dumps(SYMBOL_A)});
      const b = await mk({json.dumps(SYMBOL_B)});

      // Contribution == the buy in BOTH accounts → exactly 0.00 cash each, so
      // `portfolio_value_cad` must collapse onto `total_value_cad` to the cent.
      const buyDay = new Date(Date.now() - 30*86400000).toISOString().slice(0,10);
      const put = (account_id, security_id, action, price, quantity) =>
        post('/trades', {{account_id, security_id, action, date: buyDay,
                          quantity, price_native: price}});
      await put(tfsa, a, 'contribution', '10000.00', '0');
      await put(tfsa, a, 'buy', '100.00', '100');
      await put(rrsp, b, 'contribution', '4000.00', '0');
      await put(rrsp, b, 'buy', '20.00', '200');

      // Manual closes (manual wins, docs/07): a baseline FOUR days back with
      // nothing in between, then today — carry-forward, not "yesterday".
      const day = (n) => new Date(Date.now() - n*86400000).toISOString().slice(0,10);
      const price = (security_id, when, value) =>
        post('/prices', {{security_id, date: when, price: value}});
      await price(a, day(4), '90');
      await price(a, day(0), '100');
      await price(b, day(4), '18');
      await price(b, day(0), '20');

      const pos = await (await fetch('/api/v1/portfolio/positions',
        {{headers: auth}})).json();
      const hist = await (await fetch('/api/v1/portfolio/value-history',
        {{headers: auth}})).json();
      const sigma = (pos.items || []).reduce(
        (n, row) => n + Number(row.market_value_cad || 0), 0);
      return {{email, tfsa, rrsp, chequing, a, b,
               prior: {json.dumps(prior)},
               wire: {{
                 sigma: sigma.toFixed(2),
                 total_value_cad: pos.total_value_cad,
                 cash_cad: pos.cash_cad,
                 portfolio_value_cad: pos.portfolio_value_cad,
                 total_day_change_cad: pos.total_day_change_cad,
                 total_day_change_pct: pos.total_day_change_pct,
                 day_change_as_of: pos.day_change_as_of,
                 day_change_mixed_baseline: pos.day_change_mixed_baseline,
                 day_of_rows: (pos.items || []).map(r => r.previous_close_date),
                 hist_accounts: hist.account_ids,
                 hist_stats: hist.stats,
                 hist_buckets: (hist.buckets || []).length,
                 log: logSteps,
               }}}};
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


# The wrapper's source, in ONE place. `install_delay` runs it against the
# document that is already loaded; `arm_wrap` queues it for every document
# this tab loads after that — the difference between catching the portfolio's
# first value-history fetch and missing it. The SPA fires that request while
# the document is still reaching readyState=complete, so a probe that wraps
# fetch only once `navigate()` returns is already too late, and
# PortfolioHeaderCard's own `staleTime: 60_000` then answers any re-click
# from that unobserved cache: the card renders All's numbers while the
# wrapper never sees the response they came from. Arming the source on new
# documents makes the FIRST fetch of every window one the wrapper sees.
WRAPPER_SOURCE = """(() => {
             if (window.__m130 && window.__m130.wrapping) return 'already';
             const prev = window.__m130 || {};
             window.__m130 = Object.assign({delay: false, positions: 0}, prev,
                                            {wrapping: true});
             const orig = window.fetch.bind(window);
             window.__m130.rawFetch = orig;
             window.fetch = function (...args) {
               let url = '';
               try { url = typeof args[0] === 'string' ? args[0] : ((args[0] && args[0].url) || ''); }
               catch (e) {}
               try {
                 const hdrs = (args[1] && args[1].headers) || null;
                 let auth = null;
                 if (hdrs instanceof Headers) auth = hdrs.get('Authorization');
                 else if (hdrs) auth = hdrs.Authorization || hdrs.authorization;
                 if (auth) window.__m130.token = auth;
               } catch (e) {}
               const isPositions = url.includes('/portfolio/positions');
               if (isPositions) window.__m130.positions += 1;
               const isHistory = url.includes('/portfolio/value-history');
               const wait = (window.__m130.delay && isPositions)
                 ? new Promise(r => setTimeout(r, 5000))
                 : Promise.resolve();
               return wait.then(() => orig(...args)).then((resp) => {
                 // The sentence prints THIS response's stats, so the probe
                 // keeps the last one: DOM-vs-wire becomes DOM-vs-the-exact-
                 // response the DOM was rendered from.
                 if (isHistory && resp && resp.clone) {
                   const mirror = resp.clone();
                   mirror.json().then((j) => {
                     const entry = {
                       url, from: j.from, to: j.to,
                       granularity: j.granularity,
                       buckets: (j.buckets || []).length, stats: j.stats,
                       accounts: j.account_ids,
                     };
                     window.__m130.lastHistory = entry;
                     // EVERY response, not just the newest: the card can go
                     // back to a window React Query still holds (no request
                     // fires), so "the response this sentence was printed
                     // from" is a lookup over the list — a newest-wins read
                     // hands the check whichever window happened to be
                     // requested last.
                     const h = window.__m130.histories
                       || (window.__m130.histories = []);
                     h.unshift(entry);
                     if (h.length > 8) h.length = 8;
                   }).catch(() => {});
                 }
                 return resp;
               });
             };
             return 'installed';
           })()"""


def install_delay(tab) -> str:
    """Hold the POSITIONS refetch so the live-tick state is observable between
    the SSE patch and the server payload that replaces it (M129's trick).

    It also SNATCHES the bearer token off any request the SPA makes: the token
    lives in memory only (docs/18), so the probe reads the wire with the app's
    OWN credential instead of a second login or a second source of truth.

    Every `tab.navigate` is a REAL page load, which destroys both the wrapper
    and the state — so this is idempotent and is re-run after each navigation.
    The app's own authenticated requests refill the token within one settle."""
    return tab.evaluate(WRAPPER_SOURCE)


def arm_wrap(tab) -> str:
    """Queue WRAPPER_SOURCE for every document this tab loads from now on.

    CDP runs an init script before any page script, so `window.fetch` is
    already wrapped when the app's own module code binds it. The probe still
    calls `install_delay` after each navigation: it is idempotent
    (`already`) and doubles as the assertion that the arm held.
    """
    try:
        tab.send("Page.enable")
        tab.send("Page.addScriptToEvaluateOnNewDocument",
                 {"source": WRAPPER_SOURCE})
        return "armed"
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        return f"failed: {type(exc).__name__}: {exc}"


def click_range(tab, label: str) -> bool:
    """Click one of the header's range presets. A real user action, and the
    only reliable way to make the chart re-ask: a headless page is often not
    "focused", so React Query's refetch-on-focus does nothing."""
    wanted = json.dumps(label)
    return bool(
        tab.evaluate(
            """(() => {
                 const b = Array.from(document.querySelectorAll(
                   '[data-testid="portfolio-header"] [role="group"] button'))
                   .find(x => (x.textContent || '').trim() === WANTED);
                 if (b) { b.click(); return true; } return false;
               })()""".replace("WANTED", wanted)
        )
    )


def nudge_refetch(tab) -> str:
    """Poke React Query's window-focus refetch.

    The wrapper is installed AFTER the SPA has already booted and fired its
    first queries, so without this the capture would miss the very response
    the card rendered from — and the probe would be comparing the DOM against
    a response it never saw."""
    return str(tab.evaluate("(() => { window.dispatchEvent(new Event('focus')); window.dispatchEvent(new Event('visibilitychange')); return true; })()"))


def grab_token(tab, email: str, password: str = PASSWORD) -> str:
    """A bearer token from a REAL login.

    The snatch in `install_delay` races the SPA's first query after every page
    load, so this removes the race entirely: log in, store the token on the
    instrumented state, and let every later wire read use it. Failures are
    reported, never raised — the snatch may still have caught one, and the
    checks below test the token that actually exists, not this call."""
    return str(
        tab.evaluate(
            f"""(async () => {{
        const r = await fetch('/api/v1/auth/login',
          {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{email: {json.dumps(email)},
                                   password: {json.dumps(password)}}})}});
        const pair = await r.json().catch(() => ({{}}));
        const st = window.__m130 || (window.__m130 = {{delay: false, positions: 0,
                                                       wrapping: true}});
        st.token = pair.access_token ? ('Bearer ' + pair.access_token) : null;
        return st.token ? 'token' : ('no-token: ' + JSON.stringify(pair).slice(0, 140));
      }})()""",
            await_promise=True,
        )
    )


def arm_delay(tab, on: bool) -> None:
    tab.evaluate(f"window.__m130 && (window.__m130.delay = {'true' if on else 'false'});")


def header_state(tab) -> dict:
    """Everything the header card renders, from the DOM."""
    return tab.evaluate(
        """(() => {
             const card = document.querySelector('[data-testid="portfolio-header"]');
             const head = card && card.firstElementChild;
             const q = (sel) => card && card.querySelector(sel);
             const txt = (n) => n ? (n.textContent || '').replace(/\\s+/g, ' ').trim() : null;
             const sigma = document.querySelector('[data-testid="positions-total-row"]');
             const sigmaCells = {};
             if (sigma) {
               for (const td of sigma.querySelectorAll('td')) {
                 const sr = td.querySelector('.sr-only');
                 if (!sr) continue;
                 const label = (sr.textContent || '').trim().replace(/:/, '').trim();
                 sigmaCells[label] = (td.textContent || '')
                   .replace(sr.textContent || '', '').trim();
               }
             }
             const group = card && card.querySelector('[role="group"]');
             return {
               mounted: !!card,
               head_mounted: !!head,
               value: txt(q('[data-testid="portfolio-value"]')),
               live: !!q('[data-testid="portfolio-value"]')
                 && /live/.test(q('[data-testid="portfolio-value"]').textContent || ''),
               day: txt(q('[data-testid="portfolio-day-change"]')),
               range: txt(q('[data-testid="portfolio-range-stats"]')),
               cash: txt(q('[data-testid="portfolio-cash"]')),
               sigma: txt(sigma),
               sigma_cells: sigmaCells,
               lastHistory: (window.__m130 && window.__m130.lastHistory) || null,
               histories: (window.__m130 && window.__m130.histories) || [],
               range_buttons: group ? Array.from(group.querySelectorAll('button')).map(b => ({
                 label: (b.textContent || '').trim(),
                 pressed: b.getAttribute('aria-pressed'),
                 focusable: b.tabIndex >= 0,
               })) : [],
               range_label: group ? group.getAttribute('aria-label') : null,
               details_rows: Array.from(
                 document.querySelectorAll('details table tbody tr')).length,
               details_open: Array.from(document.querySelectorAll('details'))
                 .map(d => d.open),
             };
           })()"""
    )


def geometry(tab) -> dict:
    """Header-card clipping at the probe's width (390/412 must wrap, not clip)."""
    return tab.evaluate(
        """(() => {
             const card = document.querySelector('[data-testid="portfolio-header"]');
             const head = card && card.firstElementChild;
             if (!head) return null;
             const rect = (n) => n ? n.getBoundingClientRect() : null;
             const v = rect(card.querySelector('[data-testid="portfolio-value"]'));
             const d = rect(card.querySelector('[data-testid="portfolio-day-change"]'));
             const h = head.getBoundingClientRect();
             // Nothing in the card may leave the VIEWPORT — that is what
             // "wraps, doesn't clip" means. Anything leaving the CARD's own
             // border box is reported too, so a 2px overhang is visible in
             // the evidence instead of silently tolerated.
             let outside = null, overhang = null;
             for (const el of head.querySelectorAll('*')) {
               const r = el.getBoundingClientRect();
               if (r.width === 0 && r.height === 0) continue;
               const info = {tag: el.tagName, right: Math.round(r.right * 10) / 10,
                             cls: (el.getAttribute('class') || '').slice(0, 70)};
               if (r.right > window.innerWidth + 1 || r.left < -1) { outside = info; break; }
               if (!overhang && r.right > h.right + 0.5) overhang = info;
             }
             return {
               vw: window.innerWidth,
               head_right: Math.round(h.right * 10) / 10,
               value_right: v ? Math.round(v.right * 10) / 10 : null,
               day_right: d ? Math.round(d.right * 10) / 10 : null,
               value_bottom: v ? Math.round(v.bottom * 10) / 10 : null,
               day_top: d ? Math.round(d.top * 10) / 10 : null,
               outside, overhang,
             };
           })()"""
    )


def wait_header(tab, predicate, timeout: float = 14.0) -> dict:
    deadline, state = time.time() + timeout, {}
    while time.time() < deadline:
        state = header_state(tab)
        if predicate(state):
            return state
        tab.sleep(0.4)
    return state


def demo_summary(tab) -> dict:
    """The demo portfolio: the header plus the Σ of the served rows, DOM + wire."""
    return tab.evaluate(
        """(async () => {
             const txt = (n) => n ? (n.textContent || '').replace(/\\s+/g, ' ').trim() : null;
             const card = document.querySelector('[data-testid="portfolio-header"]');
             const q = (sel) => card && card.querySelector(sel);
             let wireSigma = null, wireTotal = null, wirePort = null, wireCash = null,
                 wireDay = null, wireAsOf = null, wirePct = null, buckets = null,
                 hasData = null, accounts = null, authed = null;
             try {
               const token = (window.__m130 && window.__m130.token) || null;
               authed = !!token;
               // The probe's OWN wire reads go through the unwrapped fetch:
               // through the wrapper they would land in `histories` as if the
               // card had rendered from them, and `?granularity=month` here
               // covers the same floor, so it would match the All sentence
               // and "the response the sentence came from" would be a
               // request the app never made.
               const F = (window.__m130 && window.__m130.rawFetch) || window.fetch;
               const H = {Authorization: token};
               const r = await F('/api/v1/portfolio/positions', {headers: H});
               if (r.ok) {
                 const pos = await r.json();
                 wireSigma = (pos.items || []).reduce(
                   (n, row) => n + Number(row.market_value_cad || 0), 0);
                 wireTotal = pos.total_value_cad;
                 wirePort = pos.portfolio_value_cad;
                 wireCash = pos.cash_cad;
                 wireDay = pos.total_day_change_cad;
                 wireAsOf = pos.day_change_as_of;
                 wirePct = pos.total_day_change_pct;
               }
               const h = await F('/api/v1/portfolio/value-history?granularity=month',
                                 {headers: H});
               if (h.ok) {
                 const hist = await h.json();
                 buckets = (hist.buckets || []).length;
                 hasData = hist.has_data;
                 accounts = hist.account_ids;
               }
             } catch (e) {}
             const sigma = document.querySelector('[data-testid="positions-total-row"]');
             const sigmaCells = {};
             if (sigma) {
               for (const td of sigma.querySelectorAll('td')) {
                 const sr = td.querySelector('.sr-only');
                 if (!sr) continue;
                 const label = (sr.textContent || '').trim().replace(/:/, '').trim();
                 sigmaCells[label] = (td.textContent || '')
                   .replace(sr.textContent || '', '').trim();
               }
             }
             return {
               mounted: !!card,
               value: txt(q('[data-testid="portfolio-value"]')),
               day: txt(q('[data-testid="portfolio-day-change"]')),
               range: txt(q('[data-testid="portfolio-range-stats"]')),
               cash: txt(q('[data-testid="portfolio-cash"]')),
               sigma: txt(sigma),
               sigma_cells: sigmaCells,
               wireSigma, wireTotal, wirePort, wireCash, wireDay, wireAsOf, wirePct,
               buckets, hasData, accounts, authed,
               lastHistory: (window.__m130 && window.__m130.lastHistory) || null,
               histories: (window.__m130 && window.__m130.histories) || [],
             };
           })()""",
        await_promise=True,
    )


def mdy(iso: str | None) -> str:
    """2026-09-09 → "Sep 9" — the label the sentence prints."""
    try:
        return datetime.strptime(iso or "", "%Y-%m-%d").strftime("%b %d").replace(" 0", " ")
    except ValueError:
        return ""


def cad_signed(value) -> str | None:
    """1400.00 → "+$1,400.00" — the signed CAD the sentence prints."""
    if value is None:
        return None
    n = float(value)
    return ("+$" if n >= 0 else "-$") + f"{abs(n):,.2f}"


def pick_history(histories, sentence) -> dict | None:
    """The captured value-history response the range sentence was printed from.

    The sentence names the two buckets it subtracted between AND the signed
    CAD move — three facts a response either carries verbatim or does not.
    The card can render an OLDER window out of React Query's cache while a
    NEWER response sits in `lastHistory`, so this is a lookup over every
    response the wrapper saw, not a read of the newest one. A window the
    server never sent matches nothing, which is the point: the sentence is
    traceable to a real response, or the check fails."""
    text = sentence or ""
    for entry in histories or []:
        stats = (entry or {}).get("stats") or {}
        change = cad_signed(stats.get("change_cad"))
        if not (stats.get("from_bucket") and stats.get("to_bucket")
                and change):
            continue
        if (mdy(stats["from_bucket"]) in text and mdy(stats["to_bucket"]) in text
                and change in text):
            return entry
    return None


def sentence_agrees(summary: dict) -> bool:
    """The headline is up AND the sentence traces to a captured response —
    the only state worth snapshotting: anything else is a card half-way
    between two windows."""
    return bool(summary.get("value")) and pick_history(
        summary.get("histories"), summary.get("range")) is not None


def iso_day(value) -> date | None:
    """2025-10-02 → date(2025, 10, 2); anything unparseable → None.

    The sentence prints its buckets WITHOUT a year ("Oct 2 → Oct 9"), so an
    age can only be read off the server's own ISO `from_bucket` — inferring
    the year from the label put the demo's 2025-10-02 floor inside 2026."""
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def range_span(text: str | None) -> tuple[datetime, datetime] | None:
    """The two dates the range sentence names — "Sep 9 → Oct 9".

    The sentence prints the buckets the server SUBTRACTED between, so checking
    their age is how the probe proves the window it claims is the window it
    got, without a second source of truth."""
    m = re.search(r"([A-Z][a-z]{2} \d{1,2})\s*→\s*([A-Z][a-z]{2} \d{1,2})", text or "")
    if not m:
        return None
    year = datetime.now(UTC).year

    def parse(part: str) -> datetime:
        try:
            return datetime.strptime(f"{part} {year}", "%b %d %Y").replace(tzinfo=UTC)
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)

    return parse(m.group(1)), parse(m.group(2))


def money_all(text: str | None) -> list[float]:
    """Every `$x,xxx.xx` in a string, signed — the cash line carries BOTH the
    holdings and the cash, so the one to reconcile against is the LAST."""
    return [
        (-1 if m.group(1) else 1) * float(m.group(2).replace(",", ""))
        for m in re.finditer(r"(-?)[+$]?([0-9,]+\.\d{2})", text or "")
    ]


def money(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(-?)[+$]?([0-9,]+\.\d{2})", text)
    if not m:
        return None
    value = float(m.group(2).replace(",", ""))
    return -value if m.group(1) == "-" else value


def wipe_profile(path: str = USER_DATA_DIR) -> str:
    """Delete the shared Chrome profile so the next Browser starts clean.

    a11y_driver pins ONE --user-data-dir for every Browser, so a second
    browser inherits the first tenant's refresh cookie, localStorage and
    IndexedDB wholesale. `Network.clearBrowserCookies` and
    `localStorage.clear()` are page-level and were measured NOT to be
    enough: the demo tab then rendered the demo's positions beside the
    throwaway's chart (a query cache keyed only by window, both tenants
    sharing the key). Wiping the directory is the only reset that starts
    the next browser as a first-time visitor. Chromium may still be
    releasing files when this runs, so a few passes, then report what is
    left rather than pretending it is clean.
    """
    for _ in range(6):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return "wiped"
        time.sleep(0.4)
    try:
        left = sorted(os.listdir(path))[:6]
    except OSError:
        left = ["<gone>"]
    return f"partial: {left}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--out", default=str(HERE / "m130-portfolio-header-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m130_portfolio_header_probe.py")
    base = args.base.rstrip("/")
    stamp = int(time.time())
    today = datetime.now(UTC).date()
    prior_iso = (today - timedelta(days=4)).isoformat()
    report: dict = {
        "base": base,
        "width": args.width,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tokens": {},
        "checks": CHECKS,
        "axe": {},
        "shots": [],
    }

    browser = Browser(width=args.width, height=args.height)
    extra_browsers: list[Browser] = []
    try:
        tab = browser.new_tab(f"{base}/login")
        tab.enable_runtime()
        report.setdefault("wrap", {})["throwaway"] = arm_wrap(tab)
        tab.navigate(f"{base}/login", timeout=45)
        settle(tab, 1.0)
        tab.force_viewport(args.width, args.height)
        # `already` is the ARM doing its job: the init script wrapped fetch
        # before this document's first line of app code ran.
        check("instrument: positions-delay wrapper armed before the SPA boots",
              str(install_delay(tab)) in ("installed", "already"),
              str(install_delay(tab)))

        seeded = seed(tab, stamp)
        wire = seeded["wire"]
        seed_log = (seeded.get("wire") or {}).get("log") or []
        bad_steps = [s for s in seed_log if s.get("status", 0) >= 400]
        check("seed: every write landed (no 4xx anywhere in the seed)",
              not bad_steps and len(seed_log) >= 10,
              json.dumps(bad_steps) if bad_steps else f"{len(seed_log)} calls ok")
        check("seed: two investment accounts + a chequing control + two CAD "
              "securities with a 4-day-below baseline",
              bool(seeded["tfsa"]) and bool(seeded["rrsp"]),
              json.dumps({k: seeded.get(k) for k in ("tfsa", "rrsp", "chequing",
                                                     "a", "b")}))

        # 2 — the WIRE, hand-checked (docs/08 §Portfolio summary).
        check("wire: Σ of the rows == total_value_cad == portfolio_value_cad "
              "== 14000.00 (cash is 0.00)",
              wire["sigma"] == "14000.00"
              and wire["total_value_cad"] == "14000.00"
              and wire["portfolio_value_cad"] == "14000.00"
              and wire["cash_cad"] == "0.00",
              json.dumps({k: wire[k] for k in
                          ("sigma", "total_value_cad", "cash_cad",
                           "portfolio_value_cad")}))
        check("wire: day Δ 1400.00 / 11.11% / as-of = the carried-forward "
              f"{prior_iso} baseline, one date across every row",
              wire["total_day_change_cad"] == "1400.00"
              and wire["total_day_change_pct"] == 11.11
              and wire["day_change_as_of"] == prior_iso
              and wire["day_change_mixed_baseline"] is False
              and wire["day_of_rows"] == [prior_iso, prior_iso],
              json.dumps({k: wire[k] for k in
                          ("total_day_change_cad", "total_day_change_pct",
                           "day_change_as_of", "day_change_mixed_baseline",
                           "day_of_rows")}))
        stats = wire["hist_stats"] or {}
        check("wire: the history scope is BOTH investment accounts and the "
              "chequing control is absent",
              sorted(wire["hist_accounts"]) == sorted([seeded["tfsa"], seeded["rrsp"]]),
              json.dumps(wire["hist_accounts"]))
        check("wire: stats.change_cad == last bucket − first bucket (the "
              "chart's own endpoints) and a real window is drawn",
              wire["hist_buckets"] >= 2
              and stats.get("from_value_cad") is not None
              and stats.get("change_cad") is not None
              and round(float(stats["to_value_cad"]) - float(stats["from_value_cad"]), 2)
              == round(float(stats["change_cad"]), 2),
              json.dumps(stats))

        ui_login(tab, base, seeded["email"])
        tab.navigate(f"{base}/investments")
        check("instrument: the wrapper was re-armed after the navigation",
              install_delay(tab) in ("installed", "already"), "armed")
        nudge_refetch(tab)  # so the capture also sees what the card rendered from
        report["tokens"]["seeded"] = grab_token(tab, seeded["email"])
        settle(tab, 3.0)
        # A real preset round-trip (1M then back to All): the app must
        # re-request each time, so the captured response is exactly the one
        # the card goes on to render.
        click_range(tab, "1M")
        settle(tab, 1.2)
        click_range(tab, "All")
        settle(tab, 1.2)
        # `"$"` in the sentence means the VALUE-HISTORY response landed too —
        # the positions payload alone cannot render it, and waiting on the
        # label alone would read a half-rendered chart. `sentence_agrees` adds
        # the third leg: the numbers on screen must be ones the wrapper
        # actually saw come off the wire.
        at_rest = wait_header(tab, lambda s: s["value"] and s["day"]
                              and "$" in (s.get("range") or "")
                              and sentence_agrees(s))

        # 3 — the header, from the DOM.
        check("header: $14,000.00 — the server's Σ, rendered",
              money(at_rest.get("value")) == 14000.0, str(at_rest.get("value")))
        check("header: ▲ +$1,400.00 (+11.11%) with NO 'live' claim at rest",
              bool(at_rest.get("day"))
              and "+$1,400.00" in at_rest["day"]
              and "(+11.11%)" in at_rest["day"]
              and not at_rest.get("live"),
              str(at_rest.get("day"))[:160])
        check(f"header: the baseline caption names the real market day "
              f"('{close_label(prior_iso)} close'), never 'yesterday'",
              bool(at_rest.get("day"))
              and f"{close_label(prior_iso)} close" in at_rest["day"]
              and "yesterday" not in at_rest["day"].lower(),
              str(at_rest.get("day"))[:160])
        at_sentence = at_rest.get("range") or ""
        at_hist = pick_history(at_rest.get("histories"), at_sentence)
        at_stats = (at_hist or {}).get("stats") or {}
        at_change = cad_signed(at_stats.get("change_cad"))
        check("header: the range sentence IS the response's stats — the two "
              "buckets and the signed change it subtracted, verbatim",
              at_hist is not None
              and bool(at_sentence)
              and "Over all time:" in at_sentence
              and mdy(at_stats.get("from_bucket")) in at_sentence
              and mdy(at_stats.get("to_bucket")) in at_sentence
              and at_change is not None and at_change in at_sentence,
              json.dumps({"range": at_sentence[:130], "stats": at_stats,
                          "captured": at_hist}))
        check("header: no cash line — zero cash needs nothing to explain",
              not at_rest.get("cash"), str(at_rest.get("cash")))

        # 4 — the Σ row prints the SAME numbers (anti-fabrication in the DOM).
        sigma_text = at_rest.get("sigma") or ""
        cells = at_rest.get("sigma_cells") or {}
        check("sigma row: the CAD total and the day Δ % equal the header's "
              "($14,000.00 / +11.11%) — one payload, one number",
              cells.get("Total market value (CAD)") == "$14,000.00"
              and cells.get("Total day change") == "+11.11%"
              and cells.get("Total unrealized (CAD)") is not None
              and money(at_rest.get("value")) == 14000.0
              and money(cells.get("Total market value (CAD)"))
              == money(at_rest.get("value")),
              json.dumps(cells))
        check("sigma row: every total it prints is NAMED (no bare number "
              "could be mistaken for a cross-currency Σ)",
              all(label in sigma_text for label in
                  ("Total market value (CAD)", "Total day change",
                   "Total unrealized (CAD)")),
              str(sigma_text)[:240])

        # 7a — the header wraps instead of clipping at this width.
        geo = geometry(tab)
        check(f"layout @{args.width}: the header card, the value and the Δ all "
              "stay inside the viewport (wrap, never clip), value stacks above Δ",
              bool(geo)
              and geo["head_right"] <= geo["vw"] + 1
              and (geo["value_right"] is None
                   or geo["value_right"] <= geo["vw"] + 1)
              and (geo["day_right"] is None or geo["day_right"] <= geo["vw"] + 1)
              and not geo["outside"]
              and (geo["day_top"] is None
                   or geo["day_top"] >= geo["value_bottom"] - 1),
              json.dumps(geo))

        shot = str(HERE / f"m130-portfolio-header-{args.width}.png")
        tab.screenshot(shot, full_page=True)
        report["shots"].append(shot)

        # 6 — the range control: a real, labelled toggle group.
        check("range: seven focusable toggles in a labelled group, one pressed",
              len(at_rest["range_buttons"]) == 7
              and at_rest["range_label"] == "Portfolio value over time range"
              and all(b["focusable"] for b in at_rest["range_buttons"])
              and sum(1 for b in at_rest["range_buttons"]
                      if b["pressed"] == "true") == 1,
              json.dumps(at_rest["range_buttons"])[:260])

        # 5 — the live tick: move the price FIRST (the refetch is still at
        # normal speed), then hold the positions refetch open so the patched
        # payload — and its `live` state — is observable before the server's
        # own numbers replace it.
        moved = tab.evaluate(
            f"""(async () => {{
                 const token = window.__m130 && window.__m130.token;
                 const auth = {{'Content-Type':'application/json',
                                Authorization: token || ''}};
                 const r = await fetch('/api/v1/prices', {{method:'POST', headers:auth,
                   body: JSON.stringify({{security_id:{seeded['a']},
                                          date:new Date().toISOString().slice(0,10),
                                          price:'110'}})}});
                 return r.status;
               }})()""",
            await_promise=True,
        )
        check("tick: POST /prices moved AAA 100 → 110 (manual wins)", moved == 201
              or moved == 200, f"status={moved}")

        arm_delay(tab, True)
        ticked = wait_header(
            tab,
            lambda s: s.get("live") and money(s.get("value")) == 15000.0,
            timeout=25,
        )
        check("tick: the header re-evaluated LIVE to $15,000.00 with the "
              "'live' badge (100×110 + 200×20, server primitives only)",
              money(ticked.get("value")) == 15000.0 and ticked.get("live"),
              f"value={ticked.get('value')} day={str(ticked.get('day'))[:120]}")
        check("tick: the day Δ re-evaluated against the SAME pinned baseline "
              "→ ▲ +$2,400.00 (+19.05%), caption unchanged",
              bool(ticked.get("day"))
              and "+$2,400.00" in ticked["day"]
              and "(+19.05%)" in ticked["day"]
              and f"{close_label(prior_iso)} close" in ticked["day"],
              str(ticked.get("day"))[:180])
        check("tick: the range sentence did NOT move — it belongs to the "
              "value-history response, which has no quote in it",
              (ticked.get("range") or "") == (at_rest.get("range") or ""),
              str(ticked.get("range"))[:160])

        # ...and the server's own payload replaces everything.
        arm_delay(tab, False)
        settled = wait_header(tab, lambda s: not s.get("live"), timeout=25)
        check("settle: the server payload landed — badge gone, $15,000.00 and "
              "+$2,400.00 now server-owned",
              not settled.get("live") and money(settled.get("value")) == 15000.0,
              f"value={settled.get('value')} day={str(settled.get('day'))[:120]}")

        # 6b — clicking a preset re-labels the sentence and re-requests.
        click_range(tab, "6M")
        six = wait_header(
            tab, lambda s: s.get("range") and "Over 6 months:" in s["range"]
            and "$" in s["range"], timeout=20
        )
        six_sentence = six.get("range") or ""
        check("range: picking 6M is a real re-request, not a relabel — the "
              "server answers with this tenant's own move (+$2,400.00 = the "
              "settled 15,000.00 minus the 12,600.00 baseline)",
              "Over 6 months:" in six_sentence
              and "+$2,400.00" in six_sentence
              and "(+19.05%)" in six_sentence
              and range_span(six_sentence) is not None,
              six_sentence[:180])
        check("range: the pressed state followed the click (one toggle, 6M)",
              sum(1 for b in six["range_buttons"] if b["pressed"] == "true") == 1
              and next(b["pressed"] for b in six["range_buttons"]
                       if b["label"] == "6M") == "true",
              json.dumps([(b["label"], b["pressed"]) for b in six["range_buttons"]]))

        # 7b — keyboard: the range group, then the chart's data-table fallback.
        # The data table only exists once the chart has its buckets, so this
        # polls rather than reading the first frame after the click.
        keys, k_deadline = {}, time.time() + 15
        while time.time() < k_deadline:
            keys = tab.evaluate(
            """(() => {
                 const group = document.querySelector(
                   '[data-testid="portfolio-header"] [role="group"]');
                 if (!group) return {ok: false, why: 'no group'};
                 const first = group.querySelector('button');
                 first.focus();
                 const start = document.activeElement === first;
                 first.nextElementSibling.focus();
                 const moved = document.activeElement !== first
                   && group.contains(document.activeElement);
                 const summary = document.querySelector('details summary');
                 if (!summary) return {ok: false, why: 'no summary', start, moved};
                 summary.focus();
                 const focusable = document.activeElement === summary;
                 summary.click();
                 const details = summary.parentElement;
                 const open = details && details.open;
                 const rows = details
                   ? details.querySelectorAll('tbody tr').length : 0;
                 return {ok: true, start, moved, focusable, open, rows};
               })()"""
            )
            if keys.get("ok"):
                break
            tab.sleep(0.5)
        check("a11y: the range toggles take focus and Tab moves between them",
              bool(keys and keys.get("start") and keys.get("moved")),
              json.dumps(keys))
        check("a11y: the chart's data-table fallback is focusable, opens, and "
              "holds the selected range's rows",
              bool(keys and keys.get("focusable") and keys.get("open")
                   and keys.get("rows", 0) > 0),
              json.dumps(keys))

        # 7c — axe, both themes, on /investments.
        def scan(tag: str) -> None:
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
        bad = sum(v["counts"]["critical"] + v["counts"]["serious"]
                  for v in report["axe"].values())
        check("axe: 0 critical + 0 serious on /investments, both themes",
              bad == 0, str(report["axe"]))

        report["console_a"] = tab.console_errors()
        check("console: no errors on the throwaway tenant",
              not report["console_a"], json.dumps(report["console_a"])[:300])

        # --- tenant B: the demo, for the REAL-portfolio acceptance ----------
        # A SEPARATE BROWSER, not a tenant switch inside the throwaway's
        # session. The two tenants share a query key
        # (`["portfolio-value-history", from, to]`) and differ only in who is
        # asking, so anything the throwaway left behind in the profile — its
        # refresh cookie, its cache — can be served to the demo's card as if it
        # were the demo's own. Measured: sharing a profile, the demo's card
        # shows the demo's holdings beside the throwaway's chart; with a clean
        # profile the card, the Σ row and the captured response all carry
        # accounts [77, 78, 79]. The app's own guard for the same hazard is
        # AuthContext's removeQueries(); the probe does not depend on it.
        # The first browser must be GONE, not just backgrounded: its page is
        # still alive and refreshing the throwaway's session, which re-writes
        # the shared profile's cookie the moment it is cleared. Closing it is
        # what actually makes the next tab a different tenant.
        try:
            browser.close()
        except Exception:  # noqa: BLE001 — already gone is fine
            pass
        # a11y_driver pins ONE --user-data-dir for every Browser, so this
        # profile still holds the throwaway's refresh cookie, localStorage
        # and IndexedDB — and a session restore on the demo's /login hands
        # the demo card the other tenant's books. Measured: the demo tab
        # rendered the demo's POSITIONS beside the THROWAWAY's chart,
        # because `["portfolio-value-history", from, to]` is keyed by window
        # alone and both tenants share that key. Closing browser1 first is
        # what makes the wipe below stick (its page would otherwise re-write
        # the cookie as it dies); clearBrowserCookies / localStorage.clear
        # after the wipe stay as belt-and-braces for anything Chromium
        # rewrote on the way out.
        report.setdefault("profile", {})["wipe"] = wipe_profile()
        browser2 = Browser(width=args.width, height=args.height)
        extra_browsers.append(browser2)
        tab = browser2.new_tab(f"{base}/login")
        tab.enable_runtime()
        report.setdefault("wrap", {})["demo"] = arm_wrap(tab)
        # The cookie jar lives in the shared profile, so it is the PAGE-level
        # Network domain that can empty it (`clearBrowserCookies` is not on
        # the browser-level session).
        tab.send("Network.enable")
        tab.send("Network.clearBrowserCookies", {})
        tab.navigate(f"{base}/login", timeout=45)
        tab.evaluate(
            """(() => { try { localStorage.clear(); sessionStorage.clear(); }
                 catch (e) {} return true; })()"""
        )
        settle(tab, 1.5)
        check("demo: the Explore-the-demo door is on the login page",
              tab.evaluate(
                  """(() => { const b = Array.from(document.querySelectorAll('button'))
                       .find(n => /Explore the demo/i.test(n.textContent || ''));
                       if (!b) return false; b.click(); return true; })()"""
              ))
        settle(tab, 3.5)
        tab.navigate(f"{base}/investments")
        check("instrument: the wrapper was re-armed for the demo tenant",
              install_delay(tab) in ("installed", "already"), "armed")
        nudge_refetch(tab)
        report["tokens"]["demo"] = grab_token(tab, DEMO_EMAIL, DEMO_PASSWORD)
        settle(tab, 3.5)
        click_range(tab, "1M")
        settle(tab, 1.2)
        click_range(tab, "All")
        settle(tab, 1.2)
        d_deadline, demo = time.time() + 25, {}
        while time.time() < d_deadline:
            try:
                demo = demo_summary(tab)
            except Exception as exc:  # noqa: BLE001 — never lose the report
                report["demo_error"] = f"{type(exc).__name__}: {exc}"[:300]
                break
            # The headline AND a response the sentence can be traced to. The
            # profile was wiped before this browser launched, so everything
            # in `histories` belongs to THIS tenant; and the SENTENCE has to
            # match, not just any response — the card can still be showing a
            # window an earlier request filled while a later one sits newest.
            if sentence_agrees(demo):
                break
            tab.sleep(0.6)

        header_value = money(demo.get("value"))
        # Decimals serialize as JSON strings — coerce once, here, so no check
        # below ever does arithmetic on a string or dies on a null.
        def num(value):
            try:
                return None if value is None else float(value)
            except (TypeError, ValueError):
                return None

        wire_total = num(demo.get("wireTotal"))
        wire_sigma = num(demo.get("wireSigma"))
        cells = demo.get("sigma_cells") or {}
        dom_sigma = money(cells.get("Total market value (CAD)"))
        # The cash line prints BOTH legs: "Holdings $X · Cash in investment
        # accounts -$Y" — the last figure is the cash, and it is ALWAYS
        # subtracted off by the header, so Σ + cash is the number it owes.
        cash_leg = money_all(demo.get("cash"))
        dom_cash = cash_leg[-1] if cash_leg else 0.0
        wire_port = num(demo.get("wirePort"))
        wire_cash = num(demo.get("wireCash"))
        check("demo: the WIRE's own Σ (sum of the rows it ships) equals its "
              "total_value_cad — no second sum anywhere",
              wire_sigma is not None and wire_total is not None
              and abs(wire_sigma - wire_total) < 0.005
              and demo.get("authed") is True,
              json.dumps({"wireSigma": wire_sigma, "wireTotal": wire_total,
                          "authed": demo.get("authed")}))
        check("demo: the header's value == the Σ row's CAD total + the cash "
              "line's cash — the two legs it names, added SERVER-side",
              header_value is not None and dom_sigma is not None
              and abs(header_value - (dom_sigma + dom_cash)) < 0.005,
              json.dumps({"header": demo.get("value"), "sigmaCell": dom_sigma,
                          "cash": dom_cash}))
        check("demo: the rendered header equals portfolio_value_cad from the "
              "wire (± one refetch) — and that equals Σ + cash on the wire too",
              header_value is not None and wire_port is not None
              and abs(header_value - wire_port) < 0.05
              and wire_sigma is not None and wire_total is not None
              and wire_cash is not None
              and abs(wire_sigma + wire_cash - wire_port) < 0.005,
              json.dumps({"header": demo.get("value"), "wirePort": wire_port,
                          "wireSigma": wire_sigma, "wireCash": wire_cash}))
        check("demo: the day change carries a real 'vs <date> close' caption "
              "(and the server names that date)",
              bool(demo.get("day")) and "close" in demo["day"]
              and bool(demo.get("wireAsOf")),
              json.dumps({"day": str(demo.get("day"))[:160],
                          "as_of": demo.get("wireAsOf"),
                          "cad": demo.get("wireDay"),
                          "pct": demo.get("wirePct")}))
        sentence = demo.get("range") or ""
        picked = pick_history(demo.get("histories"), sentence)
        hist_stats = (picked or {}).get("stats") or {}
        hist_change = cad_signed(hist_stats.get("change_cad")) or ""
        # The sentence prints THAT response's stats verbatim, so the two dates
        # and the signed change it shows must be the two buckets the server
        # subtracted between. Matched against a response the wrapper actually
        # captured, and NOT simply the newest one: the card goes back to a
        # window React Query already holds without asking again, so the newest
        # capture can be a LATER window than the sentence on screen.
        check("demo: the sentence IS the response's own stats — same two "
              "buckets, same signed change (captured off the request)",
              picked is not None
              and "Over" in sentence
              and mdy(hist_stats.get("from_bucket")) in sentence
              and mdy(hist_stats.get("to_bucket")) in sentence
              and hist_change in sentence,
              json.dumps({"sentence": sentence[:130],
                          "captured": picked,
                          "stats": hist_stats}))
        check("demo: the portfolio chart drew (has_data with buckets) over the "
              "summed investment accounts",
              demo.get("hasData") is True and (demo.get("buckets") or 0) > 1
              and len(demo.get("accounts") or []) >= 1,
              json.dumps({"buckets": demo.get("buckets"),
                          "accounts": demo.get("accounts")}))
        check("demo: the cash line appears only when there IS cash to explain",
              demo.get("cash") is None or "Cash in investment accounts" in demo["cash"],
              str(demo.get("cash"))[:160])

        shot_b = str(HERE / f"m130-demo-investments-{args.width}.png")
        tab.screenshot(shot_b, full_page=True)
        report["shots"].append(shot_b)

        # 6c — on the real portfolio the range actually changes the series.
        tab.evaluate(
            """(() => { const b = Array.from(
                 document.querySelectorAll('[data-testid="portfolio-header"] [role="group"] button'))
                 .find(x => (x.textContent || '').trim() === '1M');
                 if (b) { b.click(); return true; } return false; })()"""
        )
        demo_one_month, one_sentence, deadline = {}, "", time.time() + 20
        while time.time() < deadline:
            demo_one_month = demo_summary(tab)
            one_sentence = demo_one_month.get("range") or ""
            if "Over 1 month:" in one_sentence and sentence_agrees(demo_one_month):
                break
            tab.sleep(0.5)
        # The sentence prints its buckets WITHOUT a year ("Oct 2 → Oct 9"), so
        # the age of each window can only be read off the server's own ISO
        # `from_bucket`: inferring the year from the label dates the demo's
        # 2025-10-02 floor to 2026 and calls a 372-day history a week old.
        # Both windows come from the response their own sentence was printed
        # from, so "All" and "1M" are the server's ANSWER to each preset, not
        # the label's claim about it — and that the two differ (a month, vs
        # the whole history) is what "one range state drives the sentence and
        # the chart" means.
        all_stats = (pick_history(demo.get("histories"),
                                  demo.get("range")) or {}).get("stats") or {}
        one_stats = (pick_history(demo_one_month.get("histories"),
                                  one_sentence) or {}).get("stats") or {}
        all_from, one_from = (iso_day(all_stats.get("from_bucket")),
                              iso_day(one_stats.get("from_bucket")))
        check("demo: one range state drives both — 1M relabels the sentence "
              "AND the server answers with a window starting inside the month",
              "Over 1 month:" in one_sentence and "$" in one_sentence
              and all_from is not None and one_from is not None
              and one_from > all_from
              and (today - one_from).days <= 40
              and (today - all_from).days > 150,
              json.dumps({"all": str(demo.get("range"))[:120],
                          "1M": one_sentence[:120],
                          "all_from": all_stats.get("from_bucket"),
                          "1M_from": one_stats.get("from_bucket")}))

        scan("demo-investments")
        bad_b = sum(v["counts"]["critical"] + v["counts"]["serious"]
                    for v in report["axe"].values())
        check("axe: 0 critical + 0 serious on BOTH tenants' /investments",
              bad_b == 0, str(report["axe"]))

        report["console_b"] = tab.console_errors()
        check("console: no errors on the demo tenant",
              not report["console_b"], json.dumps(report["console_b"])[:300])

        shot_c = str(HERE / f"m130-demo-1m-{args.width}.png")
        tab.screenshot(shot_c, full_page=True)
        report["shots"].append(shot_c)
    finally:
        report["passed"] = sum(1 for c in CHECKS if c["ok"])
        report["failed"] = sum(1 for c in CHECKS if not c["ok"])
        Path(args.out).write_text(json.dumps(report, indent=2))
        print(f"\n{report['passed']} passed, {report['failed']} failed → {args.out}",
              flush=True)
        for instance in [browser, *extra_browsers]:
            try:
                instance.close()
            except Exception:  # noqa: BLE001 — evidence is already on disk
                pass
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

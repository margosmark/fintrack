"""M174 live probe — create-from-SELECTION on a phone, and the tag-intent
subscription source (AGENTS §4 step 5, running stack).

    python3 m174_parity_probe.py [--base http://127.0.0.1:4174] [--width 390]

Both halves of Mark's ask are measured against a RUNNING stack, not reasoned
about, and they run against a THROWAWAY tenant (the demo tenant answers
`403 demo_account_restricted` on every write), so nothing here touches real data.

  1  seed         register/login/account/category/tag + rows on a real backend
  2  bar@390      the bulk toolbar fits INSIDE a 390px viewport: no horizontal
                  scroll (`scrollWidth <= clientWidth`) and every control's right
                  edge inside `innerWidth` — the M174 acceptance line
  3  first        `Create from selection` is the FIRST control after the count,
                  so it is the thing you meet at 390 without hunting
  4  menu         a real menu (`role="menu"`, the pinned name) with exactly two
                  targets, opening focused on the first available one
  5  payload      both targets deep-link `?prefill=transactions` with a `q` that
                  is a case-insensitive substring of BOTH selected payees — the
                  ONE-pattern-covers-the-family guarantee, measured on the wire
  6  escape       Escape closes the menu and returns focus to the trigger
  7  rules        the deep link really prefills `/rules`: the pattern field holds
                  the derived value and the provenance line says where it came
                  from
  8  tags@390     Recategorize + Add tag are reachable at 390 as well (Mark's
                  "bulk actions like set categories or tags")
  9  tag source   three monthly charges, every one tagged with a NOMINATED tag,
                  produce a `subscription` event + count on the cash-flow
                  calendar — with no bill and no accept anywhere — and the same
                  group disappears when the setting is cleared
 10  axe         0 critical/serious with the menu OPEN at 390

Exit 0 only if every check passes; JSON + screenshots land next to this script.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import unquote_plus, urlencode

from a11y_driver import Browser
from probe_target import random_password, strip_passwords

HERE = Path(__file__).resolve().parent
AXE = str(HERE.parents[1] / "node_modules" / "axe-core" / "axe.min.js")
BASE = "https://fintrac.princessandmark.com"

CHECKS: list[dict] = []


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


def ui_login(tab, base: str, email: str, password: str) -> None:
    tab.navigate(f"{base}/login")
    settle(tab)
    tab.evaluate("""document.querySelector('input[type="email"]').focus()""")
    tab.type_text(email)
    tab.evaluate("""document.querySelector('input[type="password"]').focus()""")
    tab.type_text(password)
    tab.press("Enter")
    tab.wait_for("location.pathname !== '/login'", timeout=25)


def seed(tab) -> dict:
    """A throwaway tenant with the two shapes Mark's ask needs: a merchant with
    two spelling-identical rows (the selection) and a merchant with three
    monthly charges (the tag-intent group)."""
    stamp = int(time.time() * 1000)
    email = f"m174-{stamp}@example.com"
    password = random_password("m174")
    return tab.evaluate(
        f"""(async () => {{
      const email = {json.dumps(email)}, password = {json.dumps(password)};
      const h = {{'Content-Type': 'application/json'}};
      const reg = await fetch('/api/v1/auth/register',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const log = await fetch('/api/v1/auth/login',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const auth = {{...h, Authorization: 'Bearer ' + (await log.json()).access_token}};
      const acct = await fetch('/api/v1/accounts',
        {{method:'POST', headers:auth, body: JSON.stringify({{name:'Probe Chequing', type:'chequing', currency:'CAD'}})}});
      const account = await acct.json();
      const cat = await fetch('/api/v1/categories',
        {{method:'POST', headers:auth, body: JSON.stringify({{name:'Probe Subscriptions'}})}});
      const category = await cat.json();
      const cat2 = await fetch('/api/v1/categories',
        {{method:'POST', headers:auth, body: JSON.stringify({{name:'Probe Bulk Target'}})}});
      const category2 = await cat2.json();
      const tg = await fetch('/api/v1/tags',
        {{method:'POST', headers:auth, body: JSON.stringify({{name:'probe-recurring'}})}});
      const tag = await tg.json();
      const day = (back) => {{
        const d = new Date(); d.setDate(d.getDate() - back);
        return d.toISOString().slice(0, 10);
      }};
      const mk = async (payee, amount, back) => {{
        const r = await fetch('/api/v1/transactions', {{method:'POST', headers:auth,
          body: JSON.stringify({{account_id: account.id, date: day(back),
            raw_description: payee, normalized_payee: payee,
            amount_native: amount, currency:'CAD', category_id: category.id}})}});
        const body = await r.json();
        return {{status: r.status, id: body.id}};
      }};
      const selection = [await mk('PROBE SEAFOOD CITY SUPERMARK', '-42.10', 20),
                         await mk('PROBE SEAFOOD CITY SUPERMARK', '-51.30', 95)];
      const recurring = [await mk('PROBE NETFLIX.COM', '-15.99', 90),
                         await mk('PROBE NETFLIX.COM', '-15.99', 60),
                         await mk('PROBE NETFLIX.COM', '-15.99', 30)];
      for (const row of recurring) {{
        await fetch('/api/v1/transactions/' + row.id + '/tags', {{method:'POST', headers:auth,
          body: JSON.stringify({{tag_id: tag.id}})}});
      }}
      return {{email, password, register: reg.status, login: log.status,
              account_id: account.id, category_id: category.id, category2_id: category2.id,
              tag_id: tag.id,
              selection: selection.map(r => r.status), recurring: recurring.map(r => r.status)}};
    }})()""",
        await_promise=True,
    )


def click_button(tab, text: str) -> bool:
    return bool(
        tab.evaluate(
            f"""(() => {{
      const b = [...document.querySelectorAll('button, a')].find(
        el => el.textContent.trim() === {json.dumps(text)});
      if (!b) return false;
      b.scrollIntoView({{block: 'center'}});
      b.click();
      return true;
    }})()"""
        )
    )


def tick_row(tab, payee: str, index: int = 0) -> bool:
    """Tick the `index`-th row carrying this payee — rows of ONE merchant share
    a label, and ticking the same box twice would simply untick it."""
    return bool(
        tab.evaluate(
            f"""(() => {{
      const boxes = [...document.querySelectorAll(
        'input[aria-label=' + JSON.stringify('Select transaction: ' + {json.dumps(payee)}) + ']')];
      const box = boxes[{index}];
      if (!box) return false;
      box.scrollIntoView({{block: 'center'}});
      box.click();
      return true;
    }})()"""
        )
    )


def load_axe(tab) -> None:
    tab.evaluate(Path(AXE).read_text(encoding="utf-8"))


def axe_check(tab, name: str) -> None:
    result = tab.evaluate("axe.run(document, {resultTypes: ['violations']})", await_promise=True)
    counts = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0}
    rows: list[str] = []
    for violation in result.get("violations", []):
        impact = violation.get("impact") or "minor"
        counts[impact] = counts.get(impact, 0) + 1
        targets = [str(n.get("target")) for n in violation["nodes"][:3]]
        rows.append(f"{violation['id']}({impact}x{len(violation['nodes'])}) {targets}")
    bad = counts["critical"] + counts["serious"]
    check(f"axe 0 critical/serious — {name}", bad == 0, f"{counts} {rows}")


def tag_pipeline(tab, email: str, password: str, tag_id: int) -> dict:
    """The tag-intent source, measured end to end on the live stack.

    Reads the calendar BEFORE nominating the tag (the rows already carry it, so
    any result here would be the defect), after nominating it, and again after
    clearing the setting. Three months are scanned because the group's projected
    next occurrence is the last charge + one cadence."""
    return tab.evaluate(
        f"""(async () => {{
      const h = {{'Content-Type': 'application/json'}};
      const log = await fetch('/api/v1/auth/login', {{method:'POST', headers:h,
        body: JSON.stringify({{email: {json.dumps(email)}, password: {json.dumps(password)}}})}});
      const auth = {{...h, Authorization: 'Bearer ' + (await log.json()).access_token}};
      const monthOf = (back) => {{
        const d = new Date(); d.setDate(d.getDate() + back);
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
      }};
      const read = async () => {{
        const out = [];
        for (const month of [monthOf(0), monthOf(31), monthOf(61)]) {{
          const r = await fetch('/api/v1/reports/cashflow-calendar?month=' + month, {{headers: auth}});
          const body = await r.json();
          out.push({{month: month, count: body.confirmed_subscription_count,
                     events: body.days.flatMap(d => d.events)
                       .filter(e => e.kind === 'subscription')
                       .map(e => e.label + '|' + e.amount_cad)}});
        }}
        return out;
      }};
      const before = await read();
      const nom = await fetch('/api/v1/users/me', {{method:'PATCH', headers: auth,
        body: JSON.stringify({{recurring_intent_tag_ids: [{tag_id}]}})}});
      const nominated = (await nom.json()).recurring_intent_tag_ids;
      const after = await read();
      const cleared = await fetch('/api/v1/users/me', {{method:'PATCH', headers: auth,
        body: JSON.stringify({{recurring_intent_tag_ids: []}})}});
      const off = await read();
      return {{patchStatus: nom.status, clearStatus: cleared.status, nominated: nominated,
              before: before, after: after, off: off}};
    }})()""",
        await_promise=True,
    )


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--out", default=str(HERE / "m174-parity-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m174_parity_probe.py")
    base = args.base.rstrip("/")
    BASE = base

    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{base}/login")
        tab.enable_runtime()
        tab.send("Network.enable")
        tab.force_viewport(args.width, args.height)
        settle(tab)

        # --- 1. seed -------------------------------------------------------
        s = seed(tab)
        check(
            "seed: throwaway tenant with a 2-row family and a 3-charge group",
            s["register"] == 200
            and s["login"] == 200
            and s["selection"] == [201, 201]
            and s["recurring"] == [201, 201, 201],
            json.dumps(strip_passwords(s)),
        )
        ui_login(tab, base, s["email"], s["password"])

        # --- 2/3. the bulk bar at 390 --------------------------------------
        tab.navigate(f"{base}/transactions")
        settle(tab, 2.0)
        check(
            "selection: both PROBE SEAFOOD rows ticked",
            tick_row(tab, "PROBE SEAFOOD CITY SUPERMARK", 0)
            and tick_row(tab, "PROBE SEAFOOD CITY SUPERMARK", 1),
            "",
        )
        tab.sleep(0.6)
        bar = tab.evaluate(
            """(() => {
      const el = [...document.querySelectorAll('div')].find(
        d => d.className.includes('fixed') && d.className.includes('bottom-') && d.querySelector('[aria-live]'));
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const controls = [...el.querySelectorAll('button')].map(b => ({
        name: b.textContent.trim(), right: Math.round(b.getBoundingClientRect().right)}));
      return {scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
              left: Math.round(r.left), right: Math.round(r.right),
              innerWidth: window.innerWidth, controls,
              docScroll: document.documentElement.scrollWidth,
              docClient: document.documentElement.clientWidth};
    })()"""
        )
        check(
            "bar@390: no horizontal scroll and the bar never leaves the viewport",
            bar is not None
            and bar["scrollWidth"] <= bar["clientWidth"] + 1
            and bar["left"] >= -1
            and bar["right"] <= bar["innerWidth"] + 1,
            json.dumps(bar),
        )
        names = [c["name"] for c in (bar or {}).get("controls", [])]
        check(
            "first: 'Create from selection' is the first control after the count",
            bool(names) and names[0].startswith("Create from selection"),
            json.dumps(names),
        )
        wide = [c for c in (bar or {}).get("controls", []) if c["right"] > (bar or {}).get("innerWidth", 0) + 1]
        check(
            "reachable@390: every bulk control's right edge is inside the viewport",
            not wide,
            json.dumps(wide),
        )
        for label in ("Recategorize", "Add tag"):
            check(
                f"reachable@390: {label} present in the bar",
                any(n == label for n in names),
                json.dumps(names),
            )

        # --- 4/5/6. the menu ------------------------------------------------
        check("menu: trigger clicked", click_button(tab, "Create from selection"), "")
        tab.sleep(0.5)
        menu = tab.evaluate(
            """(() => {
      const m = document.querySelector('[role="menu"]');
      if (!m) return null;
      const items = [...m.querySelectorAll('[role="menuitem"]')];
      return {label: m.getAttribute('aria-label'),
              expanded: document.querySelector('[aria-controls="' + m.id + '"]')?.getAttribute('aria-expanded'),
              isMenu: m.getAttribute('role'),
              items: items.map(i => ({text: i.textContent.trim(), tag: i.tagName,
                                      disabled: i.getAttribute('aria-disabled'),
                                      href: i.getAttribute('href')})),
              focused: document.activeElement ? document.activeElement.textContent.trim() : null};
    })()"""
        )
        check(
            "menu: a real menu with exactly two targets, first one focused",
            menu is not None
            and menu["isMenu"] == "menu"
            and menu["label"] == "Create from the selected rows"
            and menu["expanded"] == "true"
            and len(menu["items"]) == 2
            and menu["focused"] == menu["items"][0]["text"],
            json.dumps(menu),
        )

        payloads = []
        for item in (menu or {}).get("items", []):
            href = item.get("href") or ""
            params = {}
            if "?" in href:
                for pair in href.split("?", 1)[1].split("&"):
                    key, _, value = pair.partition("=")
                    params[key] = value

            # `unquote_plus`, not `unquote`: the editors' href is built with
            # `URLSearchParams`, which encodes a space as `+`. Reading it with
            # `unquote` leaves the `+` in place and the probe then measures its
            # own parsing bug instead of the payload.
            q = unquote_plus(params.get("q", ""))
            covers = [
                q.lower() in "PROBE SEAFOOD CITY SUPERMARK".lower() for _ in range(2)
            ]
            payloads.append(
                {
                    "text": item["text"],
                    "tag": item["tag"],
                    "prefill": params.get("prefill"),
                    "q": q,
                    "covers": covers,
                    "path": href.split("?")[0],
                    "href": href,
                }
            )
        check(
            "payload: both targets carry prefill=transactions and ONE pattern covering the family",
            len(payloads) == 2
            and all(p["tag"] == "A" and p["prefill"] == "transactions" and all(p["covers"]) for p in payloads)
            and payloads[0]["q"] == payloads[1]["q"],
            json.dumps(payloads),
        )

        tab.press("Escape")
        tab.sleep(0.4)
        escaped = tab.evaluate(
            """(() => ({
      menu: document.querySelector('[role="menu"]') !== null,
      focused: document.activeElement ? document.activeElement.textContent.trim() : null}))()"""
        )
        check(
            "escape: the menu closes and focus returns to the trigger",
            escaped["menu"] is False and escaped["focused"].startswith("Create from selection"),
            json.dumps(escaped),
        )

        # --- 7. the rule target really prefills -----------------------------
        # Navigated through the link's OWN href (not a rebuilt one): what is
        # measured is the payload the menu emits, byte for byte.
        rule_href = (
            payloads[0]["href"]
            if payloads and payloads[0].get("href")
            else "/rules?" + urlencode({"prefill": "transactions", "q": ""})
        )
        tab.navigate(f"{base}{rule_href}")
        settle(tab, 1.6)
        rules = tab.evaluate(
            """(() => {
      const pattern = document.getElementById('rule-pattern');
      const prov = document.querySelector('[data-testid="prefill-provenance"]');
      return {pattern: pattern ? pattern.value : null,
              matchType: document.querySelector('#rule-match-type') ? document.querySelector('#rule-match-type').value : null,
              provenance: prov ? prov.textContent.trim() : null};
    })()"""
        )
        check(
            "rules: the deep link prefills the derived pattern and states its provenance",
            rules["pattern"] is not None
            and rules["pattern"] == (payloads[0]["q"] if payloads else None)
            and rules["provenance"] is not None
            and "Created from search" in (rules["provenance"] or ""),
            json.dumps(rules),
        )

        # The BILL half of the same acceptance: the same derived pattern, read by
        # the bill editor as its `contains` match term, with the unanimous
        # category as the matcher's scope.
        if len(payloads) > 1 and payloads[1].get("href"):
            tab.navigate(f"{base}{payloads[1]['href']}")
            settle(tab, 1.6)
            bills = tab.evaluate(
                f"""(() => {{
      // The scope select is named by aria-label, not by a linked <label>.
      const scope = document.querySelector('select[aria-label="Match category"]');
      const prov = document.querySelector('[data-testid="prefill-provenance"]');
      const term = document.querySelector(
        '[aria-label=' + JSON.stringify('Remove term — ' + {json.dumps(payloads[0]["q"])}) + ']');
      return {{provenance: prov ? prov.textContent.trim() : null,
              termPresent: Boolean(term),
              scope: scope ? scope.value : null,
              amount: (document.querySelector('input[placeholder="Amount (optional)"]') || {{}}).value}};
    }})()"""
            )
            # The category OPTIONS arrive from a query, so the select can only
            # hold the scope once they have landed — poll instead of assuming.
            scope = bills["scope"]
            for _ in range(20):
                if scope == str(s["category_id"]):
                    break
                tab.sleep(0.5)
                scope = tab.evaluate(
                    """(() => {
      const sel = document.querySelector('select[aria-label="Match category"]');
      return sel ? sel.value : null;
    })()"""
                )
            check(
                "bills: the same pattern lands as the bill's match term, scoped by the category",
                bills["termPresent"] is True
                and scope == str(s["category_id"])
                and bills["amount"] == ""
                and "Created from search" in (bills["provenance"] or ""),
                json.dumps({**bills, "scope": scope}),
            )

        # --- 8. the tag-intent source, on the live stack ---------------------
        pipeline = tag_pipeline(tab, s["email"], s["password"], int(s["tag_id"]))
        before_counts = {row["count"] for row in pipeline["before"]}
        off_counts = {row["count"] for row in pipeline["off"]}
        after_events = [
            event for row in pipeline["after"] for event in row["events"]
        ]
        check(
            "tag source: OFF (nothing nominated) despite the rows already carrying the tag",
            pipeline["patchStatus"] == 200
            and pipeline["clearStatus"] == 200
            and before_counts == {0}
            and all(row["events"] == [] for row in pipeline["before"]),
            json.dumps(pipeline["before"]),
        )
        check(
            "tag source: nominating the tag confirms the group and dates the calendar",
            {row["count"] for row in pipeline["after"]} == {1}
            and len(after_events) == 1
            and after_events[0].startswith("PROBE NETFLIX.COM|")
            and after_events[0].endswith("-15.99"),
            json.dumps(pipeline["after"]),
        )
        check(
            "tag source: clearing the setting turns it back off (nothing persisted)",
            off_counts == {0}
            and all(row["events"] == [] for row in pipeline["off"]),
            json.dumps(pipeline["off"]),
        )

        # --- 9. bulk SET CATEGORY from the bar at 390 (Mark's other half) ----
        # The toolbar's Recategorize path is pre-existing M96; what M174's
        # acceptance asks is whether a PHONE can actually complete it. Driven
        # for real: tick two rows, open the mode, pick a DIFFERENT category,
        # Apply, then read the rows back through the API.
        tab.navigate(f"{base}/transactions")
        settle(tab, 2.0)
        tick_row(tab, "PROBE SEAFOOD CITY SUPERMARK", 0)
        tick_row(tab, "PROBE SEAFOOD CITY SUPERMARK", 1)
        tab.sleep(0.6)
        check("bulk@390: Recategorize mode opened", click_button(tab, "Recategorize"), "")
        tab.sleep(0.4)
        chosen = tab.evaluate(
            f"""(() => {{
      const sel = document.getElementById('bulk-category');
      if (!sel) return null;
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLSelectElement.prototype, 'value').set;
      setter.call(sel, {json.dumps(str(s["category2_id"]))});
      sel.dispatchEvent(new Event('input', {{bubbles: true}}));
      sel.dispatchEvent(new Event('change', {{bubbles: true}}));
      const r = sel.getBoundingClientRect();
      return {{value: sel.value, right: Math.round(r.right), innerWidth: window.innerWidth}};
    }})()"""
        )
        check(
            "bulk@390: the category select is inside the viewport and holds the choice",
            chosen is not None
            and chosen["value"] == str(s["category2_id"])
            and chosen["right"] <= chosen["innerWidth"] + 1,
            json.dumps(chosen),
        )
        check("bulk@390: Apply clicked", click_button(tab, "Apply"), "")
        tab.sleep(2.0)
        applied = tab.evaluate(
            f"""(async () => {{
      const h = {{'Content-Type': 'application/json'}};
      const log = await fetch('/api/v1/auth/login', {{method:'POST', headers:h,
        body: JSON.stringify({{email: {json.dumps(s['email'])}, password: {json.dumps(s['password'])}}})}});
      const auth = {{...h, Authorization: 'Bearer ' + (await log.json()).access_token}};
      const r = await fetch('/api/v1/transactions?limit=50', {{headers: auth}});
      const body = await r.json();
      const rows = body.items.filter(t => t.normalized_payee === 'PROBE SEAFOOD CITY SUPERMARK');
      return {{status: r.status, total: body.total,
              categories: rows.map(t => t.category_id)}};
    }})()""",
            await_promise=True,
        )
        check(
            "bulk@390: the bulk set-category really landed on both selected rows",
            applied["status"] == 200
            and len(applied["categories"]) == 2
            and all(c == s["category2_id"] for c in applied["categories"]),
            json.dumps(applied),
        )

        # --- 10. axe -------------------------------------------------------
        tab.navigate(f"{base}/transactions")
        settle(tab, 2.0)
        tick_row(tab, "PROBE SEAFOOD CITY SUPERMARK", 0)
        tick_row(tab, "PROBE SEAFOOD CITY SUPERMARK", 1)
        tab.sleep(0.6)
        click_button(tab, "Create from selection")
        tab.sleep(0.5)
        load_axe(tab)
        axe_check(tab, "M174 bulk create menu at 390")
        tab.screenshot(str(HERE / "m174-390-menu.png"))
    finally:
        browser.close()

    ok = all(c["ok"] for c in CHECKS)
    Path(args.out).write_text(
        json.dumps({"base": base, "width": args.width, "checks": CHECKS}, indent=2),
        encoding="utf-8",
    )
    print(f"\n{sum(1 for c in CHECKS if c['ok'])}/{len(CHECKS)} checks passed → {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

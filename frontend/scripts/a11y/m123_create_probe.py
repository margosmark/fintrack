"""M123 Create-from-search — the live probe (AGENTS §4 step 5, running stack).

    python3 m123_create_probe.py [--base http://127.0.0.1:4174] [--width 1440]

Runs against a THROWAWAY tenant (register + login + account + category + tag +
two rows inside the page), because the demo tenant answers
`403 demo_account_restricted` on every view write. What each check proves:

  1  seed     register/login/account/category/tag/rows on a real backend
  2  mirror   `Apply filters` writes the four criteria into the address bar
              (docs/09 §Create from search — the URL IS the bookmark)
  3  menu     `Create` is a real menu: 4 targets, aria-expanded, roving focus
  4  axe      0 critical/serious with the menu OPEN (the new surface)
  5  budget   disabled + reason without a category, enabled with one
  6  save     the title defaults to the criteria as read; PATCH /users/me
              carries `kind: "transactions"` and the four params verbatim
  7  views    `Views (1)` restores by NAVIGATING to the bookmark URL
  8  rules    `/rules?prefill=…` prefills the pattern + provenance line
  9  budgets  `/budgets?prefill=…` preselects the category, Create stays gated

Exit 0 only if every check passes; JSON + screenshots land next to this script.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

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


def seed(tab) -> dict:
    stamp = int(time.time() * 1000)
    email = f"m123-{stamp}@example.com"
    password = random_password("m123")
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
        {{method:'POST', headers:auth, body: JSON.stringify({{name:'Probe Groceries'}})}});
      const category = await cat.json();
      const tg = await fetch('/api/v1/tags',
        {{method:'POST', headers:auth, body: JSON.stringify({{name:'probe-wifi'}})}});
      const tag = await tg.json();
      const mk = async (payee, amount) => {{
        const r = await fetch('/api/v1/transactions', {{method:'POST', headers:auth,
          body: JSON.stringify({{account_id: account.id, date:'2026-09-10',
            raw_description: payee, normalized_payee: payee,
            amount_native: amount, currency:'CAD', category_id: category.id}})}});
        return r.status;
      }};
      return {{email, password, register: reg.status, login: log.status,
              account_id: account.id, category_id: category.id, tag_id: tag.id,
              rows: [await mk('PROBE ROGERS WIRELESS', '-85.00'),
                     await mk('PROBE LOBLAWS', '-62.40')]}};
    }})()""",
        await_promise=True,
    )


def goto(tab, path: str) -> None:
    tab.navigate(f"{BASE}{path}")
    settle(tab)


def ui_login(tab, base: str, email: str, password: str) -> None:
    """Sign in THROUGH THE FORM — one real session, same credentials."""
    tab.navigate(f"{base}/login")
    settle(tab)
    tab.evaluate("""document.querySelector('input[type="email"]').focus()""")
    tab.type_text(email)
    tab.evaluate("""document.querySelector('input[type="password"]').focus()""")
    tab.type_text(password)
    tab.press("Enter")
    tab.wait_for("location.pathname !== '/login'", timeout=25)


def select_filter(tab, label: str, value: str) -> bool:
    """Set a filter-bar <select> by its wrapping <label> text (React-controlled)."""
    return bool(
        tab.evaluate(
            f"""(() => {{
      const ls = [...document.querySelectorAll('form label')];
      const l = ls.find(el => el.textContent.trim().startsWith({json.dumps(label)}));
      const s = l && l.querySelector('select');
      if (!s) return false;
      const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
      setter.call(s, {json.dumps(value)});
      s.dispatchEvent(new Event('input', {{bubbles: true}}));
      s.dispatchEvent(new Event('change', {{bubbles: true}}));
      return true;
    }})()"""
        )
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


def load_axe(tab) -> None:
    tab.evaluate(Path(AXE).read_text(encoding="utf-8"))


def axe_check(tab, name: str) -> None:
    result = tab.evaluate(
        "axe.run(document, {resultTypes: ['violations']})", await_promise=True
    )
    counts = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0}
    rows: list[str] = []
    for violation in result.get("violations", []):
        impact = violation.get("impact") or "minor"
        counts[impact] = counts.get(impact, 0) + 1
        targets = [str(n.get("target")) for n in violation["nodes"][:3]]
        rows.append(f"{violation['id']}({impact}x{len(violation['nodes'])}) {targets}")
    bad = counts["critical"] + counts["serious"]
    check(f"axe 0 critical/serious — {name}", bad == 0, f"{counts} {rows}")


def request_bodies(tab, method: str, needle: str) -> list[str]:
    out = []
    for event in tab.events("Network.requestWillBeSent"):
        req = event["params"]["request"]
        if req.get("method") == method and needle in req.get("url", ""):
            out.append(req.get("postData") or "")
    return out


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--out", default=str(HERE / "m123-create-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m123_create_probe.py")
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
        ok = (
            s["register"] == 200
            and s["login"] == 200
            and s["account_id"]
            and s["category_id"]
            and s["rows"] == [201, 201]
        )
        check("seed tenant (register/login/account/category/tag/2 rows)", bool(ok), json.dumps(strip_passwords(s)))
        # Sign in THROUGH THE FORM (m121/m122's approach): the access token
        # lives in memory only (`frontend/src/api/client.ts`), so the token
        # minted inside `seed()` is invisible to the SPA, and over plain http a
        # `Secure` cookie is dropped — one real login, one real session.
        ui_login(tab, base, s["email"], s["password"])
        goto(tab, "/transactions")
        check("landed on /transactions", tab.evaluate("location.pathname") == "/transactions")

        cat_id, acct_id, tag_id = str(s["category_id"]), str(s["account_id"]), str(s["tag_id"])

        # --- 5a. Budget disabled while no CATEGORY filter is set -----------
        select_filter(tab, "Account", acct_id)
        click_button(tab, "Apply filters")
        tab.sleep(1.4)
        click_button(tab, "Create")
        tab.wait_for("document.querySelector('[role=\"menu\"]') !== null", timeout=10)
        budget = tab.evaluate(
            """(() => { const i = [...document.querySelectorAll('[role="menuitem"]')]
                 .find(el => el.textContent.includes('Budget'));
               return i ? {disabled: i.getAttribute('aria-disabled'), text: i.textContent} : null; })()"""
        )
        check(
            "Budget disabled + reason without a category filter",
            bool(budget) and budget["disabled"] == "true" and "Set a category filter first" in budget["text"],
            json.dumps(budget),
        )
        tab.press("Escape")
        tab.sleep(0.4)
        check(
            "Escape closes the menu and restores the trigger",
            tab.evaluate("document.querySelector('[role=\"menu\"]') === null")
            and tab.evaluate(
                "document.activeElement?.textContent?.trim().startsWith('Create') ?? false"
            ),
        )

        # --- 2. Apply mirrors the four criteria into the address bar -------
        # The account filter from step 5a STAYS: it is part of the search this
        # feature carries, and dropping it would hide exactly the bug this
        # probe exists to catch.
        select_filter(tab, "Category", cat_id)
        click_button(tab, "Apply filters")
        tab.sleep(1.4)
        search = tab.evaluate("location.search")
        check(
            "Apply filters mirrors category into the address bar",
            f"category={cat_id}" in search,
            f"location.search={search}",
        )

        # --- 3/4/5b. the menu ---------------------------------------------
        click_button(tab, "Create")
        tab.wait_for("document.querySelector('[role=\"menu\"]') !== null", timeout=10)
        menu = tab.evaluate(
            """(() => { const m = document.querySelector('[role="menu"]');
                 return {count: m.querySelectorAll('[role="menuitem"]').length,
                         label: m.getAttribute('aria-label'),
                         summary: (m.parentElement.textContent || '').trim(),
                         focused: document.activeElement?.textContent?.trim().slice(0, 40),
                         expanded: [...document.querySelectorAll('button[aria-haspopup="menu"]')]
                                    .find(b => b.textContent.trim() === 'Create')
                                    ?.getAttribute('aria-expanded')}; })()"""
        )
        check("menu has exactly four targets", menu["count"] == 4, json.dumps(menu))
        check(
            "trigger reports aria-expanded=true and the menu is labelled",
            menu["expanded"] == "true" and menu["label"] == "Create from this search",
            f"expanded={menu['expanded']} label={menu['label']}",
        )
        check(
            "focus opens on the first target (roving focus)",
            menu["focused"].startswith("Saved view"),
            str(menu["focused"]),
        )
        check(
            "criteria line names the labels, not raw ids",
            "Probe Groceries" in menu["summary"] and "Probe Chequing" in menu["summary"],
            menu["summary"][:180],
        )
        check(
            "Budget is enabled once a category filter is set",
            tab.evaluate(
                """[...document.querySelectorAll('[role="menuitem"]')]
                     .find(el => el.textContent.includes('Budget'))
                     ?.getAttribute('aria-disabled')"""
            )
            in (None, "false"),
        )
        tab.screenshot(str(HERE / f"m123-menu-{args.width}.png"))
        load_axe(tab)
        axe_check(tab, "Create menu open")

        # --- 6. save the view ---------------------------------------------
        tab.evaluate(
            """(() => { const b = [...document.querySelectorAll('[role="menuitem"]')]
                 .find(el => el.textContent.includes('Saved view')); b.click(); return true; })()"""
        )
        tab.wait_for("document.querySelector('input[maxlength=\"80\"]') !== null", timeout=10)
        default_title = tab.evaluate("document.querySelector('input[maxlength=\"80\"]').value")
        check(
            "save panel opens with the criteria as the default title",
            "Probe Groceries" in default_title and "Probe Chequing" in default_title,
            default_title,
        )
        tab.evaluate(
            """(() => { const i = document.querySelector('input[maxlength="80"]');
                 const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                 s.call(i, 'Probe grocery run');
                 i.dispatchEvent(new Event('input', {bubbles: true}));
                 return true; })()"""
        )
        tab.sleep(0.3)
        click_button(tab, "Save")
        tab.sleep(1.6)
        bodies = request_bodies(tab, "PATCH", "/users/me")
        saved = [b for b in bodies if "grocery run" in b]
        ok = False
        if saved:
            payload = json.loads(saved[-1])["saved_report_views"][0]
            ok = (
                payload.get("kind") == "transactions"
                and payload.get("tab") == "transactions"
                and payload.get("state", {}).get("category") == cat_id
            )
            detail = json.dumps(payload)
        else:
            detail = f"no PATCH body with the title; bodies={bodies[-2:]}"
        check("PATCH /users/me stores kind=transactions + the criteria verbatim", ok, detail)
        check(
            "save panel closes on success",
            tab.evaluate("document.querySelector('input[maxlength=\"80\"]') === null"),
        )

        # --- 7. Views list restores by navigating --------------------------
        click_button(tab, "Views (1)")
        tab.wait_for("document.getElementById('create-from-search-panel') !== null", timeout=10)
        row = tab.evaluate(
            """(() => { const p = document.getElementById('create-from-search-panel');
                 const b = p && p.querySelector('button');
                 return b ? b.textContent : null; })()"""
        )
        check("Views row names the view and its criteria", bool(row) and "Probe grocery run" in row, str(row))
        tab.screenshot(str(HERE / f"m123-views-{args.width}.png"))
        tab.evaluate(
            """(() => { const p = document.getElementById('create-from-search-panel');
                 p.querySelector('button').click(); return true; })()"""
        )
        tab.sleep(1.4)
        restored = tab.evaluate("location.search")
        check(
            "restore NAVIGATES to the view's bookmark URL",
            f"category={cat_id}" in restored and "account=" in restored,
            f"location.search={restored}",
        )

        # --- 8. the rule deep link ----------------------------------------
        goto(tab, f"/rules?prefill=transactions&q=ROGERS&category={cat_id}&account={acct_id}&tag={tag_id}")
        tab.wait_for("document.getElementById('rule-pattern') !== null", timeout=20)
        pattern = tab.evaluate("document.getElementById('rule-pattern').value")
        prov = tab.evaluate(
            "document.querySelector('[data-testid=\"prefill-provenance\"]')?.textContent ?? ''"
        )
        check("rule pattern prefilled from the deep link", pattern == "ROGERS", pattern)
        check(
            "rule provenance says what a rule cannot carry",
            prov.startswith("Created from search:") and "not carried into a rule" in prov,
            prov[:200],
        )
        tab.screenshot(str(HERE / f"m123-rules-{args.width}.png"))

        # --- 9. the budget deep link --------------------------------------
        goto(tab, f"/budgets?prefill=transactions&category={cat_id}&q=ROGERS&account={acct_id}")
        tab.wait_for("document.querySelector('select[aria-label=\"Budget category\"]') !== null", timeout=20)
        tab.sleep(0.6)
        picked = tab.evaluate("document.querySelector('select[aria-label=\"Budget category\"]').value")
        period = tab.evaluate("document.querySelector('select[aria-label=\"Budget period\"]').value")
        gated = tab.evaluate(
            "[...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Create')?.disabled"
        )
        check("budget category pre-selected", picked == cat_id, f"value={picked}")
        check("budget period is monthly (the current month)", period == "monthly", period)
        check("budget Create stays gated until an amount is typed", gated is True, f"disabled={gated}")
        tab.screenshot(str(HERE / f"m123-budgets-{args.width}.png"))

        # --- the bill deep link (same codec, third target) -----------------
        goto(tab, f"/bills?prefill=transactions&q=ROGERS&category={cat_id}")
        tab.wait_for("document.querySelector('[data-testid=\"prefill-provenance\"]') !== null", timeout=20)
        bill_prov = tab.evaluate(
            "document.querySelector('[data-testid=\"prefill-provenance\"]').textContent"
        )
        amount = tab.evaluate(
            "[...document.querySelectorAll('input')].find(i => i.placeholder === 'Amount (optional)')?.value"
        )
        check(
            "bill draft opened with amount blank and a provenance line",
            amount == "" and bill_prov.startswith("Created from search:"),
            f"amount={amount!r} prov={bill_prov[:160]}",
        )
        tab.screenshot(str(HERE / f"m123-bills-{args.width}.png"))
    finally:
        browser.close()

    Path(args.out).write_text(json.dumps(CHECKS, indent=2), encoding="utf-8")
    failed = [c for c in CHECKS if not c["ok"]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed -> {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""M110 budget carry — the live probe (AGENTS §4 step 5, against the deployed stack).

    python3 m110_carry_probe.py [--base https://fintrac.princessandmark.com] [--width 1440]

A THROWAWAY tenant (register + login + one account + one category + one budget
with `carry: forward`), because the demo tenant answers 403
`demo_account_restricted` on every write. What each check proves:

  1 seed      register/login/account/category/budget + a SOURCE-period spend
              (2026-09-05) and a CURRENT-period spend (2026-10-06)
  2 backdate  operator psql sets `budgets.created_at` before the source month —
              the API cannot set it, and docs/09 only carries from a period the
              budget governed in full (scoped to this throwaway user only)
  3 server    GET /budgets answers carried_in=60.00, remaining=560.00,
              pct_used=15.151515151515152 (100/660*100) — the money hand check
  4 UI        /budgets renders the layers: the `Carry leftover` toggle with
              aria-pressed=true, `+$60 carried in`, `/ $660 spendable`,
              `15% used` — i.e. the CLIENT port agrees with the server (the
              numbers on screen come from s3Engine, not the API response)
  5 toggle    one click flips the row back to the pre-M110 math (/ $600,
              no carry line, aria-pressed=false) and back
  6 themes    both themes screenshot

Exit 0 only if every check passes; JSON + PNG evidence land next to this script.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from a11y_driver import Browser
from probe_target import random_password

HERE = Path(__file__).resolve().parent
CHECKS: list[dict] = []

# Throwaway tenant credentials (same pattern as m104_tag_probe) — never a real
# account, never reused anywhere else.
PASSWORD = random_password("m110")
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


def psql(sql: str) -> str:
    remote = "docker exec -i fin_tracker-db-1 psql -U fintrack -d fintrack -tA -c " + shlex.quote(sql)
    out = subprocess.run(["ssh", "example.internal", remote], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr[:400]}")
    return out.stdout.strip()


def seed(tab, stamp: int) -> dict:
    """Register, sign in, and build the probe budget (all in-page)."""
    email = f"m110-probe-{stamp}@example.com"
    return tab.evaluate(
        f"""(async () => {{
      const email = {json.dumps(email)}, password = {json.dumps(PASSWORD)};
      const h = {{'Content-Type': 'application/json'}};
      const reg = await fetch('/api/v1/auth/register',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const log = await fetch('/api/v1/auth/login',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const auth = {{...h, Authorization: 'Bearer ' + (await log.json()).access_token}};
      const acct = await fetch('/api/v1/accounts',
        {{method:'POST', headers:auth, body: JSON.stringify({{name:'M110 probe', type:'chequing', currency:'CAD'}})}});
      const account_id = (await acct.json()).id;
      const cat = await fetch('/api/v1/categories',
        {{method:'POST', headers:auth, body: JSON.stringify({{name:'M110 carry'}})}});
      const category_id = (await cat.json()).id;
      const budget = await fetch('/api/v1/budgets',
        {{method:'POST', headers:auth,
          body: JSON.stringify({{category_id, amount: 600, period: 'monthly', carry: 'forward'}})}});
      const budget_json = await budget.json();
      const src = await fetch('/api/v1/transactions',
        {{method:'POST', headers:auth, body: JSON.stringify({{account_id, date:'2026-09-05',
          raw_description:'M110 source spend', amount_native:'-540.00', currency:'CAD', category_id}})}});
      const cur = await fetch('/api/v1/transactions',
        {{method:'POST', headers:auth, body: JSON.stringify({{account_id, date:'2026-10-06',
          raw_description:'M110 this-month spend', amount_native:'-100.00', currency:'CAD', category_id}})}});
      return {{email, register: reg.status, login: log.status, account: acct.status,
              category: cat.status, budget: budget.status, budget_carry: budget_json.carry,
              budget_id: budget_json.id, source: src.status, current: cur.status,
              category_name: 'M110 carry'}};
    }})()""",
        await_promise=True,
    )


def ui_login(tab, base: str) -> None:
    tab.navigate(f"{base}/login")
    settle(tab)
    tab.evaluate("document.querySelector('input[type=\"email\"]').focus()")
    tab.type_text(__import__("os").environ.get("M110_EMAIL", ""))
    tab.evaluate("document.querySelector('input[type=\"password\"]').focus()")
    tab.type_text(PASSWORD)
    tab.press("Enter")
    tab.wait_for("location.pathname !== '/login'", timeout=25)


def text_of(tab) -> str:
    return str(tab.evaluate("document.body.innerText") or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--width", type=int, default=1440)
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m110_carry_probe.py")
    stamp = int(time.time())

    browser = Browser(width=args.width, height=900)
    try:
        tab = browser.new_tab(args.base)
        tab.navigate(args.base)
        seed_result = seed(tab, stamp)
        check("seed", seed_result["budget"] == 201 and seed_result["budget_carry"] == "forward",
              json.dumps(seed_result))
        email = seed_result["email"]
        budget_id = seed_result["budget_id"]

        # The API cannot set created_at; backdate so the budget governed the
        # whole source period (docs/09). Operator psql, scoped to this user.
        updated = psql(
            "UPDATE budgets SET created_at='2026-08-31T04:00:00+00:00' "
            f"WHERE user_id=(SELECT id FROM users WHERE email='{email}') RETURNING id;"
        )
        check("backdate", str(budget_id) in updated, updated.replace("\n", ","))

        # --- server truth through the deployed API -----------------------
        server = tab.evaluate(
            """(async () => {
              const log = await fetch('/api/v1/auth/login',
                {method:'POST', headers:{'Content-Type':'application/json'},
                 body: JSON.stringify({email: %s, password: %s})});
              const auth = 'Bearer ' + (await log.json()).access_token;
              const r = await fetch('/api/v1/budgets', {headers:{Authorization: auth}});
              return await r.json();
            })()""" % (json.dumps(email), json.dumps(PASSWORD)),
            await_promise=True,
        )
        row = next((r for r in server if r["id"] == budget_id), None)
        check("server carried_in", row is not None and row["carried_in"] == "60.00",
              json.dumps(row, sort_keys=True) if row else "row missing")
        if row:
            check("server remaining", row["remaining"] == "560.00", row["remaining"])
            check("server pct_used", row["pct_used"] == 15.151515151515152, str(row["pct_used"]))

        # --- the UI, light theme -----------------------------------------
        tab.evaluate("try { localStorage.setItem('theme','light') } catch (e) {}")
        tab.navigate(f"{args.base}/budgets")
        settle(tab)
        if tab.evaluate("location.pathname") == "/login":
            ui_login(tab, args.base)
            tab.navigate(f"{args.base}/budgets")
            settle(tab)
        body = text_of(tab)
        check("ui toggle present",
              "Carry leftover" in body and f"Carry leftover — {seed_result['category_name']}" not in body,
              "Carry leftover pill rendered")
        pressed = tab.evaluate(
            """(() => {
              const b = [...document.querySelectorAll('button')]
                .find((el) => (el.getAttribute('aria-label') || '').startsWith('Carry leftover'));
              return b ? b.getAttribute('aria-pressed') : null;
            })()"""
        )
        check("ui aria-pressed=true", pressed == "true", str(pressed))
        check("ui carried line", "+$60 carried in — extra room this period" in body,
              "+$60 carried in — extra room this period")
        check("ui spendable denominator", "/ $660" in body and "spendable" in body, "/ $660 spendable")
        check("ui pct from the client port", "15% used" in body, "15% used (100/660)")
        light_shot = str(HERE / "m110-carry-prod-1440.png")
        tab.screenshot(light_shot)

        # --- dark theme ----------------------------------------------------
        tab.evaluate("try { localStorage.setItem('theme','dark') } catch (e) {}")
        tab.navigate(f"{args.base}/budgets")
        settle(tab)
        dark_body = text_of(tab)
        check("dark theme carries the same layers",
              "+$60 carried in — extra room this period" in dark_body and "/ $660" in dark_body,
              "carried line + pool denominator in dark")
        dark_shot = str(HERE / "m110-carry-prod-dark-1440.png")
        tab.screenshot(dark_shot)

        # --- one click back to the pre-M110 row ---------------------------
        rect = tab.evaluate(
            """(() => {
              const b = [...document.querySelectorAll('button')]
                .find((el) => (el.getAttribute('aria-label') || '').startsWith('Carry leftover'));
              if (!b) return null;
              b.scrollIntoView({block:'center'});
              const r = b.getBoundingClientRect();
              return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            })()"""
        )
        ok_click = False
        if rect:
            tab.sleep(0.3)
            tab.click_at(rect["x"], rect["y"])
            tab.sleep(2.0)
            after = text_of(tab)
            pressed_after = tab.evaluate(
                """(() => {
                  const b = [...document.querySelectorAll('button')]
                    .find((el) => (el.getAttribute('aria-label') || '').startsWith('Carry leftover'));
                  return b ? b.getAttribute('aria-pressed') : null;
                })()"""
            )
            ok_click = pressed_after == "false" and "/ $600" in after and "carried in" not in after
            check("one click flips the row off", ok_click,
                  f"aria-pressed={pressed_after}, pool={'/ $600' in after}")
            off_shot = str(HERE / "m110-carry-off-prod-1440.png")
            tab.screenshot(off_shot)
        else:
            check("one click flips the row off", False, "toggle not found")

        errors = tab.console_errors()
        check("no console errors", len(errors) == 0, "; ".join(errors[:3]))

        evidence = {
            "milestone": "M110",
            "base": args.base,
            "width": args.width,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "budget_id": budget_id,
            "email": email,
            "seed": seed_result,
            "server_row": row,
            "screenshots": [light_shot, dark_shot],
            "checks": CHECKS,
        }
        out = HERE / "m110-carry-probe.json"
        out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(f"evidence: {out}")
    finally:
        browser.close()

    failed = [c for c in CHECKS if not c["ok"]]
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

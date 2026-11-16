"""M106 — the History drawer on the ACCOUNT ACTIVITY surface (AGENTS §4 step 5).

`m106_review_probe.py` exercises `/transactions`, which is where the drawer was
born; this card's change is the second list that edits a row, so this probe runs
the SAME two entry points on `/accounts/{id}` against the deployed stack and
proves the acceptance clauses live:

  row cell   History -> the drawer opens with THIS row's receipts only,
             newest first, the unicode category sentence verbatim
  editor     History -> the editor hands the row over and closes first
             (exactly one dialog mounted, and it is the drawer)
  axe        0 critical/serious with the drawer open

    python3 m106_account_history_probe.py [--base https://fintrac.princessandmark.com]

Exit 0 only if every check passes; JSON evidence lands next to this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import a11y_driver  # noqa: E402
import m106_review_probe as P  # noqa: E402  (the house glue: settle/goto/click/axe)
from probe_target import random_password

OK: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    OK.append((bool(ok), f"{name} — {detail}"))
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


SEED_JS = """
(async () => {
  const h = {'Content-Type': 'application/json'};
  const email = EMAIL, password = PASSWORD;
  const reg = await fetch('/api/v1/auth/register',
    {method:'POST', headers:h, body: JSON.stringify({email, password})});
  const log = await fetch('/api/v1/auth/login',
    {method:'POST', headers:h, body: JSON.stringify({email, password})});
  const tok = (await log.json()).access_token;
  const auth = {...h, Authorization: 'Bearer ' + tok};
  const inst = await fetch('/api/v1/institutions',
    {method:'POST', headers:auth, body: JSON.stringify({name: 'Probe Bank'})});
  const instId = (await inst.json()).id;
  const acct = await fetch('/api/v1/accounts',
    {method:'POST', headers:auth,
     body: JSON.stringify({name: 'Probe Chequing', type: 'chequing',
                           currency: 'CAD', institution_id: instId})});
  const account = await acct.json();
  const tx = await fetch('/api/v1/transactions',
    {method:'POST', headers:auth,
     body: JSON.stringify({account_id: account.id, date: '2026-09-10',
                           raw_description: 'PROBE LOB Laws',
                           normalized_payee: 'Loblaws',
                           amount_native: '-42.10', currency: 'CAD'})});
  const row = await tx.json();
  // a receipt the drawer must print, including a unicode category name
  const cat = await fetch('/api/v1/categories',
    {method:'POST', headers:auth, body: JSON.stringify({name: 'Café ☕ 食品 😀'})});
  const category = await cat.json();
  await fetch('/api/v1/transactions/' + row.id,
    {method:'PATCH', headers:auth, body: JSON.stringify({category_id: category.id})});
  return {register: reg.status, login: log.status, account_id: account.id,
          tx_id: row.id, token: tok,
          events: await (await fetch('/api/v1/transactions/' + row.id + '/events',
            {headers: auth})).json()};
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--out", default=str(HERE / "m106-account-history.json"))
    ap.add_argument("--shot", default=str(HERE / "m106-account-history-drawer.png"))
    args = ap.parse_args()
    P.BASE = args.base.rstrip("/")
    from probe_target import require_target
    require_target(args.base, who="m106_account_history_probe.py")

    email = f"m106acct-{int(time.time() * 1000)}@example.com"
    password = random_password("m106acct")
    browser = a11y_driver.Browser(width=1440, height=900)
    try:
        tab = browser.new_tab(f"{P.BASE}/login")
        tab.enable_runtime()
        tab.force_viewport(1440, 900)
        P.settle(tab)

        seeded = tab.evaluate(
            SEED_JS.replace("EMAIL", json.dumps(email)).replace(
                "PASSWORD", json.dumps(password)
            ),
            await_promise=True,
        )
        check("seed tenant + account + row + unicode category receipt",
              seeded and seeded.get("register") == 200 and seeded.get("login") == 200,
              json.dumps({k: v for k, v in (seeded or {}).items() if k != "token"}))
        if not seeded or not seeded.get("account_id"):
            return 1
        account_id, tx_id = seeded["account_id"], seeded["tx_id"]
        kinds = [e["event_kind"] for e in (seeded.get("events") or {}).get("items", [])]
        check("the row already has categorized over created (newest first)",
              kinds == ["categorized", "created"], str(kinds))
        check("unicode summary served over the wire",
              any("食品" in (e.get("summary") or "")
                  for e in (seeded.get("events") or {}).get("items", [])),
              json.dumps([e.get("summary") for e in
                          (seeded.get("events") or {}).get("items", [])],
                         ensure_ascii=False))

        # --- be the SPA: land logged in -------------------------------------
        P.goto(tab, f"/accounts/{account_id}")
        if tab.evaluate("location.pathname") == "/login":
            P.ui_login(tab, email, password)
            P.goto(tab, f"/accounts/{account_id}")
        check("on the account activity page",
              tab.evaluate("location.pathname") == f"/accounts/{account_id}",
              str(tab.evaluate("location.pathname")))

        label = f"History — Loblaws (transaction {tx_id})"
        handle = tab.wait_for(
            f"!!document.querySelector('[aria-label={json.dumps(label)}]')", timeout=25
        )
        check("row Actions cell carries the History control", bool(handle), label)

        # (a) row cell -> drawer
        P.click_button(tab, f'[aria-label="{label}"]')
        tab.wait_for("!!document.querySelector('[role=dialog]')", timeout=10)
        # The receipts are a query behind the dialog: wait for the SENTENCE,
        # not the shell, before reading the body (a skeleton is not evidence).
        loaded = tab.wait_for(
            "document.querySelector('[role=dialog]') !== null && "
            "document.querySelector('[role=dialog]').innerText.indexOf('set category') >= 0",
            timeout=20,
        )
        check("drawer fetched this row's receipts", bool(loaded), "sentence rendered")
        lines = tab.evaluate(
            "(() => [...document.querySelectorAll('[role=dialog] li')]"
            ".map(li => li.innerText.replace(/\\s+/g, ' ').trim()))()"
        ) or []
        row_text = tab.evaluate(
            "document.querySelector('[role=dialog]')?.innerText || ''"
        )
        check("row History opens the drawer", "History" in (row_text or ""),
              (row_text or "").splitlines()[:2])
        check("drawer lists THIS row's receipts, newest first",
              len(lines) == 2
              and "set category Café ☕ 食品 😀" in lines[0]
              and "created this transaction" in lines[1],
              json.dumps(lines, ensure_ascii=False))
        tab.screenshot(args.shot)
        P.click_button(tab, '[aria-label="Close history"]')
        tab.sleep(0.4)

        # (b) edit modal -> drawer, one dialog at a time
        P.click_button(tab, f'[aria-label="Edit transaction {tx_id}"]')
        tab.wait_for("!!document.querySelector('[aria-label=\"History for this transaction\"]')",
                     timeout=10)
        P.click_button(tab, '[aria-label="History for this transaction"]')
        tab.wait_for("!!document.querySelector('#history-drawer-title')", timeout=10)
        handed_receipts = tab.wait_for(
            "document.querySelectorAll('[role=dialog] li').length >= 1", timeout=20
        )
        check("…and it arrives with this row's receipts, not an empty shell",
              bool(handed_receipts),
              tab.evaluate("[...document.querySelectorAll('[role=dialog] li')]"
                           ".map(li => li.innerText.replace(/\\s+/g,' ').trim())"))
        handed = tab.evaluate(
            """(() => {
              const dialogs = [...document.querySelectorAll('[role=dialog]')];
              return {n: dialogs.length,
                      titled: dialogs.filter(d => d.querySelector('#history-drawer-title')).length};
            })()"""
        )
        check("editor hands the row over — exactly one dialog, and it is the drawer",
              handed == {"n": 1, "titled": 1}, json.dumps(handed))

        P.load_axe(tab)
        P.axe_check(tab, "account activity with the drawer open")
        axe = P.CHECKS[-1]
        check(axe["check"], axe["ok"], axe["detail"])
        return 0 if all(ok for ok, _ in OK) else 1
    finally:
        Path(args.out).write_text(
            json.dumps(
                {"base": P.BASE, "checks": [{"ok": ok, "detail": d} for ok, d in OK]},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"evidence -> {args.out}  screenshot -> {args.shot}")
        browser.close()


if __name__ == "__main__":
    sys.exit(main())

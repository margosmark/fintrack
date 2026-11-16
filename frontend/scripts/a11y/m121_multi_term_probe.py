"""M121 multi-term match rules — the live probe (AGENTS §4 step 5, deployed stack).

    python3 m121_multi_term_probe.py [--base https://fintrac.princessandmark.com] [--width 390]

A THROWAWAY tenant (register + login + one account + one CSV import), because
the demo tenant answers 403 `demo_account_restricted` on every write. What
each check proves, in the order the card's acceptance states it:

  1 seed      five statement rows — `ROGERS`, `ROGERS COMMUNICATIONS`, `RCOM`,
              `ROGERS WIRELESS` and a decoy `FIDO` — then a bill created with
              ONE term (`["ROGERS"]`): the pre-M121 answer, so every widening
              below has something to be wider than.
  2 ANY       `PUT /bills/{id}/rule` with TWO alias terms under `any` → the
              matched set WIDENS (3 → 4: `RCOM` joins) and every number moves
              together — list `payment_count`, detail `matched_count`, the
              chart total (4 x $50 = $200.00), the rolling window and the
              paginated `transactions.total` (still 4 under `limit=2`).
  3 ALL       the SAME two terms under `all` → the set NARROWS to 0 (no payee
              carries both aliases) and every number says 0 honestly; then an
              intersecting pair (`ROGERS` + `WIRELESS`) narrows to exactly the
              one row that carries both.
  4 wire      `match_term == match_terms[0]` on the list row (the pre-M121
              field never lies), 422 `validation_error` for an 11th term and
              for a self-disagreeing `match_term`.
  5 UI        `/bills/:id` -> **Edit rule**: the chips render (one per term),
              `ANY` / `ALL` read as WORDS beside the field with `aria-selected`
              on the right one, the header joins the terms with `and`, and the
              keyboard lap works for real — type + `Enter` adds a chip,
              `Backspace` on the EMPTY field removes the last one.
  6 axe       0 violations at 390 and 1440 in BOTH themes, with the rule
              editor OPEN (that is where the new control lives).
  7 shots     screenshots of every width x theme.

Exit 0 only if every check passes; JSON + PNG evidence land next to this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from a11y_driver import Browser
from axe_scan import load_axe, run_axe, set_theme, summarize
from probe_target import random_password

CHECKS: list[dict] = []

# Throwaway tenant credentials (same pattern as the m104/m116 probes) — never
# a real account, never reused anywhere else.
PASSWORD = random_password("m121")
# One row per statement line: payee, month, amount. FIDO shares no token with
# any rule term, so it is the decoy the matched set must never contain.
ROWS = [
    ("ROGERS", 5, 50),
    ("ROGERS COMMUNICATIONS", 6, 50),
    ("RCOM", 7, 50),
    ("ROGERS WIRELESS", 8, 50),
    ("FIDO", 6, 75),
]
DECOY = "FIDO"
YEAR = 2026

# Hand-computed expectations, asserted as literals so a refactor that counts
# the wrong population fails here.
SINGLE_TERM_COUNT = 3  # ROGERS + ROGERS COMMUNICATIONS + ROGERS WIRELESS
ANY_COUNT = 4  # + RCOM
ALL_CONFLICTING_COUNT = 0  # no payee carries both ROGERS and RCOM
ALL_INTERSECTING_COUNT = 1  # ROGERS WIRELESS carries both
ANY_CHART_TOTAL = "200.00"  # 4 x 50.00
# The chart buckets May..Aug (4 periods) under ANY: (50+50+50+50)/4 = 50.00.
ANY_ROLLING_MEAN = "50.00"


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


# --------------------------------------------------------------------------- #
# 1. seed — register, import, create the ONE-term bill
# --------------------------------------------------------------------------- #

SEED_JS = """
(async () => {
  const email = __EMAIL__, password = __PASSWORD__;
  const h = {'Content-Type': 'application/json'};
  await fetch('/api/v1/auth/register',
    {method:'POST', headers:h, body: JSON.stringify({email, password})});
  const log = await fetch('/api/v1/auth/login',
    {method:'POST', headers:h, body: JSON.stringify({email, password})});
  const token = (await log.json()).access_token;
  const auth = {...h, Authorization: 'Bearer ' + token};
  const acct = await fetch('/api/v1/accounts',
    {method:'POST', headers:auth, body: JSON.stringify({name:'M121 probe', type:'chequing', currency:'CAD'})});
  const account_id = (await acct.json()).id;

  const form = new FormData();
  form.append('file', new Blob([__CSV__], {type:'text/csv'}), 'm121.csv');
  form.append('account_id', String(account_id));
  form.append('profile_json', JSON.stringify({
    columns: {date: 'Date', amount_native: 'Amount', raw_description: 'Description'},
    date_format: '%Y-%m-%d', credits_positive: true,
  }));
  // ONLY the Authorization header: a JSON Content-Type would overwrite the
  // multipart boundary and the server would see an empty body (422).
  const up = await fetch('/api/v1/imports/upload',
    {method:'POST', headers:{Authorization: auth.Authorization}, body: form});
  const up_json = await up.json();
  const batch_id = up_json?.batch?.id ?? up_json?.id ?? null;
  let commit_status = 0;
  if (batch_id) {
    const commit = await fetch('/api/v1/imports/' + batch_id + '/commit',
      {method:'POST', headers:auth});
    commit_status = commit.status;
  }

  // ONE term — the pre-M121 answer — sent through the NEW `match_terms` field,
  // so every widening below is measured against a real baseline.
  const bill = await fetch('/api/v1/bills', {method:'POST', headers:auth,
    body: JSON.stringify({
      name: 'Rogers', frequency: 'monthly', next_due_date: '__YEAR__-09-05',
      match_mode: 'contains', match_terms: ['ROGERS'], match_logic: 'any',
    })});
  const bill_json = await bill.json();
  const detail = await (await fetch('/api/v1/bills/' + bill_json.id + '/detail',
    {headers: auth})).json();
  const listed = await (await fetch('/api/v1/bills', {headers: auth})).json();

  return {
    email, account_id, token,
    bill_id: bill_json.id ?? null,
    bill_status: bill.status,
    bill_body: bill_json,
    upload_status: up.status,
    upload_body: JSON.stringify(up_json).slice(0, 300),
    batch_id, commit_status,
    detail, list_row: listed[0] ?? null,
  };
})()
"""


def seed(tab, stamp: int) -> dict:
    """Register, sign in, IMPORT a statement (docs/03), create the bill.

    The rows come through `POST /imports/upload` + `/commit` — the real import
    flow, not hand-posted transactions — so the matched set is proven against
    the pipeline that actually produces a ledger. `profile_json` is the
    signed-CSV profile a probe account needs (it has no institution).
    """
    csv_rows = ["Date,Description,Amount"]
    for payee, month, amount in ROWS:
        csv_rows.append(f"{YEAR}-{month:02d}-05,{payee},-{amount}.00")
    csv_body = "\n".join(csv_rows) + "\n"
    email = f"m121-probe-{stamp}@example.com"
    js = (
        SEED_JS.replace("__EMAIL__", json.dumps(email))
        .replace("__PASSWORD__", json.dumps(PASSWORD))
        .replace("__CSV__", json.dumps(csv_body))
        .replace("__YEAR__", str(YEAR))
    )
    return tab.evaluate(js, await_promise=True)


# --------------------------------------------------------------------------- #
# 2/3. the rule edits — every number the page shows, re-read after each PUT
# --------------------------------------------------------------------------- #

RULE_JS = """
(async () => {
  const bearer = ['Bearer', __TOKEN__].join(' ');
  const auth = {'Content-Type': 'application/json', Authorization: bearer};
  const billId = __BILL_ID__;
  const put = await fetch('/api/v1/bills/' + billId + '/rule',
    {method:'PUT', headers:auth,
     body: JSON.stringify({match_mode:'contains', match_terms:__TERMS__,
                           match_logic:__LOGIC__})});
  const detail = await (await fetch('/api/v1/bills/' + billId + '/detail',
    {headers: auth})).json();
  const paged = await (await fetch('/api/v1/bills/' + billId + '/detail?limit=2',
    {headers: auth})).json();
  const listed = await (await fetch('/api/v1/bills', {headers: auth})).json();
  const txn_rows = await (await fetch('/api/v1/transactions?limit=5',
    {headers: auth})).json();
  return {put_status: put.status,
          put_body: put.status === 200 ? null : (await put.text()).slice(0, 300),
          detail, paged, list_row: listed[0] ?? null,
          txn_total: txn_rows.total};
})()
"""


def put_rule(tab, token: str, bill_id: int, terms: list[str], logic: str) -> dict:
    js = (
        RULE_JS.replace("__TOKEN__", json.dumps(token))
        .replace("__BILL_ID__", str(int(bill_id)))
        .replace("__TERMS__", json.dumps(terms))
        .replace("__LOGIC__", json.dumps(logic))
    )
    return tab.evaluate(js, await_promise=True)


VALIDATION_JS = """
(async () => {
  const bearer = ['Bearer', __TOKEN__].join(' ');
  const auth = {'Content-Type': 'application/json', Authorization: bearer};
  const billId = __BILL_ID__;
  const many = [];
  for (let i = 0; i < 11; i++) many.push('TERM' + i);
  const over = await fetch('/api/v1/bills/' + billId + '/rule',
    {method:'PUT', headers:auth, body: JSON.stringify({match_terms: many})});
  const disagree = await fetch('/api/v1/bills/' + billId + '/rule',
    {method:'PUT', headers:auth,
     body: JSON.stringify({match_term:'ROGERS', match_terms:['FIDO']})});
  const digits = await fetch('/api/v1/bills/' + billId + '/rule',
    {method:'PUT', headers:auth, body: JSON.stringify({match_terms:['123']})});
  const bad_logic = await fetch('/api/v1/bills/' + billId + '/rule',
    {method:'PUT', headers:auth,
     body: JSON.stringify({match_terms:['ROGERS'], match_logic:'maybe'})});
  const body = async (r) => (await r.text()).slice(0, 4000);
  return {
    many: [over.status, await body(over)],
    disagree: [disagree.status, await body(disagree)],
    digits: [digits.status, await body(digits)],
    bad_logic: [bad_logic.status, await body(bad_logic)],
  };
})()
"""


def validation_checks(tab, token: str, bill_id: int) -> dict:
    js = VALIDATION_JS.replace("__TOKEN__", json.dumps(token)).replace(
        "__BILL_ID__", str(int(bill_id))
    )
    return tab.evaluate(js, await_promise=True)


def ui_login(tab, base: str, email: str) -> None:
    """Sign in THROUGH THE FORM — the access token lives in memory only
    (frontend/src/api/client.ts), so a token minted in `seed()` is invisible to
    the SPA. Same credentials, same tenant, one real session."""
    tab.navigate(f"{base}/login")
    settle(tab)
    tab.evaluate("""document.querySelector('input[type="email"]').focus()""")
    tab.type_text(email)
    tab.evaluate("""document.querySelector('input[type="password"]').focus()""")
    tab.type_text(PASSWORD)
    tab.press("Enter")
    tab.wait_for("location.pathname !== '/login'", timeout=25)


OPEN_EDITOR_JS = """
(() => {
  const btn = Array.from(document.querySelectorAll('button'))
    .find(b => (b.getAttribute('aria-label') || '').startsWith('Edit rule'));
  if (!btn) return {opened: false};
  btn.click();
  return {opened: true};
})()
"""


def editor_state(tab) -> dict:
    """Everything the open rule editor shows: chips, ANY/ALL, the header line."""
    return tab.evaluate(
        """(() => {
      const list = document.querySelector('ul[aria-label="Match terms"]');
      const tabs = Array.from(document.querySelectorAll('[role="tab"]'))
        .map(t => ({label: (t.textContent || '').trim(),
                    selected: t.getAttribute('aria-selected')}));
      const input = document.querySelector('input[id]');
      const ruleLine = Array.from(document.querySelectorAll('p,span,div'))
        .map(n => (n.textContent || '').trim())
        .find(t => t.startsWith('Matches transactions whose payee')) ?? null;
      return {
        chips: list ? Array.from(list.querySelectorAll('li'))
                      .map(li => (li.firstChild?.textContent || '').trim()) : [],
        chip_remove_labels: list ? Array.from(list.querySelectorAll('button'))
                                   .map(b => b.getAttribute('aria-label')) : [],
        tabs,
        input_name: input ? (document.querySelector('label[for="' + input.id + '"]')
                                ?.textContent || '').trim() : null,
        hint: (document.querySelector('[id$="-hint"]')?.textContent || '').trim(),
        rule_line: ruleLine,
        page_text: (document.body.innerText || '').slice(0, 4000),
      };
    })()"""
    )


def keyboard_lap(tab) -> dict:
    """Type + Enter adds a chip; Backspace on the EMPTY field removes one.

    Every step YIELDS before the next key: React flushes an `input` event's
    state through the scheduler, so firing `keydown` in the same synchronous
    task would reach the PREVIOUS `draft` and prove nothing at all (measured:
    the first run's Enter was a no-op for exactly that reason).
    """
    return tab.evaluate(
        """(async () => {
      const input = document.querySelector('input[id]');
      const label = document.querySelector('label[for="' + input.id + '"]');
      const chips = () => Array.from(
        document.querySelectorAll('ul[aria-label="Match terms"] li'))
        .map(li => (li.firstChild?.textContent || '').trim());
      const wait = (ms) => new Promise(r => setTimeout(r, ms));
      const fire = (key) => input.dispatchEvent(
        new KeyboardEvent('keydown', {key, bubbles: true, cancelable: true}));
      const set = (v) => {
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype, 'value').set;
        setter.call(input, v);
        input.dispatchEvent(new Event('input', {bubbles: true}));
      };
      const before = chips();
      set('RCOM');
      await wait(80);
      const typed = input.value;
      fire('Enter');
      await wait(80);
      const cleared = input.value;
      const after_enter = chips();
      // Backspace on the now-empty field must delete the LAST chip only.
      fire('Backspace');
      await wait(80);
      const after_backspace = chips();
      fire('Backspace');
      await wait(80);
      const after_second = chips();
      return {
        input_name: (label?.textContent || '').trim(),
        typed, cleared, before, after_enter, after_backspace, after_second,
      };
    })()""",
        await_promise=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--out", default=str(HERE / "m121-multi-term-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m121_multi_term_probe.py")
    base = args.base.rstrip("/")
    stamp = int(time.time())

    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{base}/login")
        tab.enable_runtime()
        tab.navigate(f"{base}/login", timeout=45)
        settle(tab, 1.0)

        seeded: dict = seed(tab, stamp)
        check(
            "seed: statement uploaded through /imports/upload",
            seeded["upload_status"] in (200, 201),
            f"status={seeded['upload_status']} batch={seeded['batch_id']} "
            f"{seeded['upload_body'][:200]}",
        )
        check(
            "seed: batch committed (the rows are real ledger rows now)",
            seeded["commit_status"] in (200, 201),
            str(seeded["commit_status"]),
        )
        check(
            "seed: bill created from a ONE-term `match_terms` body",
            seeded["bill_status"] == 201 and bool(seeded["bill_id"]),
            f"status={seeded['bill_status']} id={seeded['bill_id']}",
        )
        body: dict = seeded.get("bill_body") or {}
        check(
            "wire: create echoes match_terms / match_logic / match_term=terms[0]",
            body.get("match_terms") == ["ROGERS"]
            and body.get("match_logic") == "any"
            and body.get("match_term") == "ROGERS",
            f"terms={body.get('match_terms')} logic={body.get('match_logic')} "
            f"term={body.get('match_term')}",
        )

        detail: dict = seeded.get("detail") or {}
        check(
            f"single term: matched_count == {SINGLE_TERM_COUNT} (decoy excluded)",
            detail.get("matched_count") == SINGLE_TERM_COUNT,
            str(detail.get("matched_count")),
        )
        listed: dict = seeded.get("list_row") or {}
        check(
            "list row agrees with the detail page",
            listed.get("payment_count") == SINGLE_TERM_COUNT
            and listed.get("match_terms") == ["ROGERS"],
            f"payment_count={listed.get('payment_count')} "
            f"terms={listed.get('match_terms')}",
        )

        bill_id = seeded["bill_id"]
        token = seeded["token"]

        # ---- 2. ANY widens -------------------------------------------------
        any_run: dict = put_rule(tab, token, bill_id, ["ROGERS", "RCOM"], "any")
        report: dict = {
            "base": base,
            "bill_id": bill_id,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "checks": CHECKS,
            "axe": {},
        }
        check(
            "ANY: PUT /rule accepted",
            any_run.get("put_status") == 200,
            str(any_run.get("put_body")),
        )
        any_detail: dict = any_run["detail"]
        any_bill: dict = any_detail.get("bill", {})
        check(
            f"ANY: matched_count widens {SINGLE_TERM_COUNT} -> {ANY_COUNT} (RCOM joins)",
            any_detail.get("matched_count") == ANY_COUNT,
            str(any_detail.get("matched_count")),
        )
        check(
            "ANY: lifetime payment_count widened with it",
            any_bill.get("payment_count") == ANY_COUNT,
            str(any_bill.get("payment_count")),
        )
        points: list = any_detail.get("chart", {}).get("points", [])
        total = sum(float(p["total"]) for p in points)
        check(
            f"ANY: chart total sums to {ANY_CHART_TOTAL}",
            f"{total:.2f}" == ANY_CHART_TOTAL,
            f"{total:.2f} over {len(points)} periods",
        )
        rolling: dict = any_detail.get("rolling", {}).get("twelve", {})
        check(
            f"ANY: 12-period mean hand-computed {ANY_ROLLING_MEAN} "
            "(4 periods x $50 / 4)",
            rolling.get("mean") == ANY_ROLLING_MEAN,
            f"mean={rolling.get('mean')} payments={rolling.get('payments')}",
        )
        paged: dict = any_run["paged"]
        check(
            "ANY: pagination reports the TRUE set, not the page size (limit=2 → total 4)",
            paged.get("matched_count") == ANY_COUNT
            and paged.get("transactions", {}).get("total") == ANY_COUNT
            and len(paged.get("transactions", {}).get("items", [])) == 2,
            f"matched={paged.get('matched_count')} "
            f"total={paged.get('transactions', {}).get('total')} "
            f"items={len(paged.get('transactions', {}).get('items', []))}",
        )
        any_list: dict = any_run["list_row"] or {}
        check(
            "ANY: the list row moved too",
            any_list.get("payment_count") == ANY_COUNT
            and any_list.get("match_terms") == ["ROGERS", "RCOM"]
            and any_list.get("match_logic") == "any",
            f"count={any_list.get('payment_count')} terms={any_list.get('match_terms')}",
        )
        decoy = [
            t["normalized_payee"]
            for t in any_detail.get("transactions", {}).get("items", [])
            if t["normalized_payee"] == DECOY
        ]
        check("ANY: the decoy never joins the set", decoy == [], str(decoy))

        # ---- 3. ALL narrows ------------------------------------------------
        all_run: dict = put_rule(tab, token, bill_id, ["ROGERS", "RCOM"], "all")
        all_detail: dict = all_run["detail"]
        all_bill: dict = all_detail.get("bill", {})
        all_list: dict = all_run["list_row"] or {}
        check(
            f"ALL: matched_count narrows {ANY_COUNT} -> {ALL_CONFLICTING_COUNT}",
            all_detail.get("matched_count") == ALL_CONFLICTING_COUNT,
            str(all_detail.get("matched_count")),
        )
        check(
            "ALL: every other number says zero honestly",
            all_bill.get("payment_count") == 0
            and all_list.get("payment_count") == 0
            and all_detail.get("transactions", {}).get("total") == 0
            and all_detail.get("rolling", {}).get("twelve", {}).get("payments") == 0,
            f"payment_count={all_bill.get('payment_count')} "
            f"list={all_list.get('payment_count')} "
            f"page_total={all_detail.get('transactions', {}).get('total')}",
        )

        narrow_run: dict = put_rule(tab, token, bill_id, ["ROGERS", "WIRELESS"], "all")
        narrow_detail: dict = narrow_run["detail"]
        items = narrow_detail.get("transactions", {}).get("items", [])
        check(
            f"ALL: an intersecting pair narrows to exactly "
            f"{ALL_INTERSECTING_COUNT} row",
            narrow_detail.get("matched_count") == ALL_INTERSECTING_COUNT
            and [t["normalized_payee"] for t in items] == ["ROGERS WIRELESS"],
            f"count={narrow_detail.get('matched_count')} "
            f"payees={[t['normalized_payee'] for t in items]}",
        )

        # ---- 4. wire: legacy field + the 422s ------------------------------
        narrow_list: dict = narrow_run["list_row"] or {}
        check(
            "wire: match_term is still terms[0] on the list row",
            narrow_list.get("match_term") == "ROGERS"
            and narrow_list.get("match_terms") == ["ROGERS", "WIRELESS"]
            and narrow_list.get("match_logic") == "all",
            f"term={narrow_list.get('match_term')} terms={narrow_list.get('match_terms')} "
            f"logic={narrow_list.get('match_logic')}",
        )
        invalid: dict = validation_checks(tab, token, bill_id)
        check(
            "wire: an 11th term is 422 validation_error",
            invalid["many"][0] == 422 and "validation_error" in invalid["many"][1],
            str(invalid["many"]),
        )
        check(
            "wire: a self-disagreeing match_term is 422 validation_error",
            invalid["disagree"][0] == 422
            and "validation_error" in invalid["disagree"][1],
            str(invalid["disagree"]),
        )
        check(
            "wire: a digits-only term is 422 validation_error",
            invalid["digits"][0] == 422 and "validation_error" in invalid["digits"][1],
            str(invalid["digits"]),
        )
        check(
            "wire: an unknown match_logic is 422",
            invalid["bad_logic"][0] == 422,
            str(invalid["bad_logic"]),
        )

        # ---- 5. the UI (with the rule editor open) -------------------------
        ui_login(tab, base, seeded["email"])
        settle(tab, 2.0)
        tab.navigate(f"{base}/bills/{bill_id}")
        settle(tab, 2.0)

        opened: dict = tab.evaluate(OPEN_EDITOR_JS)
        check("UI: Edit rule opened", opened.get("opened") is True, str(opened))
        settle(tab, 1.0)
        state: dict = editor_state(tab)
        report["ui"] = state
        check(
            "UI: one chip per term, in order",
            state.get("chips") == ["ROGERS", "WIRELESS"],
            str(state.get("chips")),
        )
        check(
            "UI: every chip has a labelled remove button",
            state.get("chip_remove_labels")
            == ["Remove term — ROGERS", "Remove term — WIRELESS"],
            str(state.get("chip_remove_labels")),
        )
        tabs = state.get("tabs") or []
        labels = [t.get("label") for t in tabs]
        check(
            "UI: ANY/ALL are WORDS beside the field, ALL selected (current rule)",
            labels == ["ANY", "ALL"]
            and [t.get("selected") for t in tabs] == ["false", "true"],
            json.dumps(tabs),
        )
        check(
            "UI: the input is named 'Match terms' and the hint states the contract",
            (state.get("input_name") or "").startswith("Match terms")
            and "Backspace" in (state.get("hint") or "")
            and "up to 10 terms" in (state.get("hint") or ""),
            f"name={state.get('input_name')} hint={state.get('hint')}",
        )
        check(
            "UI: the header joins the terms with `and` (ALL)",
            "contains “ROGERS” and “WIRELESS”" in (state.get("rule_line") or ""),
            str(state.get("rule_line")),
        )

        lap: dict = keyboard_lap(tab)
        report["keyboard"] = lap
        check(
            "keyboard: the field is named 'Match terms'",
            lap.get("input_name", "").startswith("Match terms"),
            str(lap.get("input_name")),
        )
        check(
            "keyboard: Enter commits the typed term as a chip and clears the field",
            lap.get("typed") == "RCOM"
            and lap.get("cleared") == ""
            and lap.get("after_enter") == ["ROGERS", "WIRELESS", "RCOM"],
            json.dumps(lap),
        )
        check(
            "keyboard: Backspace on the EMPTY field removes the last chip",
            lap.get("after_backspace") == ["ROGERS", "WIRELESS"],
            str(lap.get("after_backspace")),
        )
        check(
            "keyboard: a second Backspace removes the next chip",
            lap.get("after_second") == ["ROGERS"],
            str(lap.get("after_second")),
        )

        # ---- 6/7. axe + screenshots (editor OPEN) --------------------------
        load_axe(tab)
        for theme in ("dark", "light"):
            for width, height in ((390, 844), (1440, 900)):
                got = tab.force_viewport(width, height)
                if got[0] != width:
                    check(f"viewport {width} pinned", False, str(got))
                    continue
                tab.navigate(f"{base}/bills/{bill_id}")
                settle(tab, 1.6)
                opened = tab.evaluate(OPEN_EDITOR_JS)
                settle(tab, 1.0)
                set_theme(tab, theme)
                tab.sleep(0.5)
                load_axe(tab)
                tab.evaluate(
                    "new Promise(r => { let y = 0; const step = () => { y += 600;"
                    " window.scrollTo(0, y);"
                    " if (y < document.body.scrollHeight) requestAnimationFrame(step);"
                    " else { window.scrollTo(0,0); r(true);} };"
                    " requestAnimationFrame(step); })",
                    await_promise=True,
                )
                tab.sleep(0.4)
                result = run_axe(tab)
                key = f"bill-rule-editor:{width}:{theme}"
                report["axe"][key] = summarize(result["violations"])
                report["axe"][key]["viewport"] = tab.viewport()
                report["axe"][key]["editor_open"] = bool(opened.get("opened"))
                counts: dict = report["axe"][key]["counts"]
                check(
                    f"axe {key} = 0 violations",
                    sum(counts.values()) == 0,
                    f"critical={counts['critical']} serious={counts['serious']} "
                    f"moderate={counts['moderate']} minor={counts['minor']}",
                )
                tab.screenshot(str(HERE / f"m121-rule-{width}-{theme}.png"))

        # ---- evidence: leave the rule on the WIDENED shape -----------------
        final: dict = put_rule(tab, token, bill_id, ["ROGERS", "RCOM"], "any")
        report["final_rule"] = {
            "matched_count": final["detail"].get("matched_count"),
            "terms": (final["list_row"] or {}).get("match_terms"),
            "logic": (final["list_row"] or {}).get("match_logic"),
        }
        check(
            "final: the widened rule is what the evidence points at",
            report["final_rule"]["matched_count"] == ANY_COUNT
            and report["final_rule"]["terms"] == ["ROGERS", "RCOM"]
            and report["final_rule"]["logic"] == "any",
            json.dumps(report["final_rule"]),
        )

        Path(args.out).write_text(json.dumps(report, indent=2))
        failed = [c for c in CHECKS if not c["ok"]]
        print(
            f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed -> {args.out}",
            flush=True,
        )
        for c in failed:
            print(f"  FAILED: {c['check']} — {c['detail']}", flush=True)
        # The throwaway tenant is left in place on purpose: deleting it would
        # erase the rows the evidence points at.
        return 1 if failed else 0
    finally:
        browser.close()


if __name__ == "__main__":
    raise SystemExit(main())

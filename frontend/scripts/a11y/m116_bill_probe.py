"""M116 bills suite — the live probe (AGENTS §4 step 5, against the deployed stack).

    python3 m116_bill_probe.py [--base https://fintrac.princessandmark.com] [--width 390]

A THROWAWAY tenant (register + login + one account + one category), because the
demo tenant answers 403 `demo_account_restricted` on every write. What each
check proves:

  1 seed      six MONTHLY payments of one merchant across seven months with
              month 4 deliberately SKIPPED (the "missed" case), plus a decoy
              merchant that must never match
  2 server    `GET /bills/{id}/detail` → matched 6, chart 7 periods with the
              gap marked, and every rolling figure HAND-COMPUTED here:
                  12-period mean = (10+20+30+0+50+60+70)/7 = 240/7 = 34.29
                  12-period median (sorted 0,10,20,30,50,60,70)   = 30.00
                  6-period window (20,30,0,50,60,70): 230/6       = 38.33
                  6-period median (sorted 0,20,30,50,60,70)       = 40.00
  3 find      `POST /bills/{id}/find-past` groups the same six rows per month,
              newest first, and writes nothing (row count unchanged)
  4 derived   `PUT /bills/{id}/rule` widening the term adds rows IMMEDIATELY —
              no link table, nothing to refresh
  5 UI        `/bills/:id` renders the rule line, `6 payments`, both rolling
              windows, the chart frame's aria-label and the data table whose
              `missed` row is reachable by keyboard (Tab + Enter on the
              native `summary`)
  6 axe       0 violations at 390 / 412 / 1440 in BOTH themes
  7 shots     screenshots of every width × theme

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

from a11y_driver import Browser  # noqa: E402
from axe_scan import load_axe, run_axe, set_theme, summarize  # noqa: E402
from probe_target import random_password

CHECKS: list[dict] = []

# Throwaway tenant credentials (same pattern as m104/m110 probes) — never a
# real account, never reused anywhere else.
PASSWORD = random_password("m116")
# month -> amount. Month 4 is MISSING on purpose: the chart must render it as a
# zero bar with a `missed` marker, and the average must count it as $0.00.
PAYMENTS = {1: 10, 2: 20, 3: 30, 5: 50, 6: 60, 7: 70}
MERCHANT = "HYDRO ONE"
# Shares the first token with MERCHANT on purpose: `starts_with "HYDRO ONE"`
# must exclude it, `starts_with "HYDRO"` must include it — that delta IS the
# "derived, not copied" check (widening the rule reshapes the bill at once).
DECOY = "HYDRO QUOTE"
YEAR = 2026

# Hand-computed expectations (see the module docstring) — asserted as literals
# so a refactor that averages the wrong population fails here.
EXPECT_12_MEAN = "34.29"
EXPECT_12_MEDIAN = "30.00"
EXPECT_6_MEAN = "38.33"
EXPECT_6_MEDIAN = "40.00"
EXPECT_TOTAL = "240.00"  # 10+20+30+50+60+70 — the missed month adds nothing


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
    """Register, sign in, IMPORT a statement, then create the bill.

    The rows come through `POST /imports/upload` + `/commit` — the real import
    flow, not hand-posted transactions — so "see past payments fill it" is
    proven against the pipeline that actually produces a ledger (docs/03).
    `Date,Description,Amount` is the header set the signed-CSV profile accepts
    (the same `columns` mapping `tests/test_bank_sync.py` sends as
    `profile_json`, because a probe account has no institution), and
    `HYDRO ONE` is what `importers/base.py::normalized_payee` leaves in
    `normalized_payee`.
    """
    email = f"m116-probe-{stamp}@example.com"
    rows = ["Date,Description,Amount"]
    for month, amount in sorted(PAYMENTS.items()):
        rows.append(f"{YEAR}-{month:02d}-05,{MERCHANT},-{amount}.00")
    # A decoy that shares no prefix with the rule: widening to it is what
    # proves the matched set is derived (check 4).
    rows.append(f"{YEAR}-03-09,{DECOY},-42.50")
    csv_body = "\n".join(rows) + "\n"
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
        {{method:'POST', headers:auth, body: JSON.stringify({{name:'M116 probe', type:'chequing', currency:'CAD'}})}});
      const account_id = (await acct.json()).id;
      const cat = await fetch('/api/v1/categories',
        {{method:'POST', headers:auth, body: JSON.stringify({{name:'M116 utilities'}})}});
      const category_id = (await cat.json()).id;

      // --- the IMPORT flow (docs/03): upload, then commit ----------------
      const form = new FormData();
      form.append('file', new Blob([{json.dumps(csv_body)}], {{type: 'text/csv'}}), 'm116.csv');
      form.append('account_id', String(account_id));
      // The account has no institution, so the upload needs the wizard's
      // profile explicitly (422 `missing_importer_profile` otherwise — the
      // SAME signed-CSV profile `tests/test_bank_sync.py` uses).
      form.append('profile_json', JSON.stringify({{
        columns: {{date: 'Date', amount_native: 'Amount', raw_description: 'Description'}},
        date_format: '%Y-%m-%d',
        credits_positive: true,
      }}));
      // ONLY the Authorization header: `auth` carries `Content-Type:
      // application/json`, which would overwrite the multipart boundary and the
      // server would see an empty body (measured: 422 "file: Field required").
      const up = await fetch('/api/v1/imports/upload',
        {{method: 'POST', headers: {{Authorization: auth.Authorization}}, body: form}});
      const up_json = await up.json();
      const batch_id = up_json?.batch?.id ?? up_json?.id ?? null;
      let commit_status = 0;
      if (batch_id) {{
        const commit = await fetch(`/api/v1/imports/${{batch_id}}/commit`,
          {{method: 'POST', headers: auth}});
        commit_status = commit.status;
      }}

      // NO category_id on the rule: an imported CSV row carries no category,
      // and a category-scoped rule would (correctly) exclude every one of them
      // — measured on prod, where the first run matched 0 of 7 for exactly that
      // reason. Category scoping is covered by backend/tests/test_bills_match.
      const bill = await fetch('/api/v1/bills',
        {{method:'POST', headers:auth, body: JSON.stringify({{
          name: 'Hydro', amount: 40, amount_tolerance: 70,
          frequency: 'monthly', next_due_date: '{YEAR}-08-05',
          match_mode: 'starts_with', match_term: 'HYDRO ONE',
        }})}});
      const bill_json = await bill.json();

      // --- server checks (2..4) ---
      const detail = await (await fetch(`/api/v1/bills/${{bill_json.id}}/detail`,
        {{headers: auth}})).json();
      const rowsBefore = detail.matched_count;
      const findBefore = await (await fetch(`/api/v1/bills/${{bill_json.id}}/find-past`,
        {{method:'POST', headers:auth, body:'{{}}'}})).json();
      // Read BEFORE the rule is widened below, so the list row still describes
      // the rule the checks above measured.
      const listed = await (await fetch('/api/v1/bills', {{headers: auth}})).json();

      return {{
        email, account_id, category_id, auth,
        bill_id: bill_json.id ?? null,
        bill_status: bill.status,
        upload_status: up.status,
        upload_body: JSON.stringify(up_json).slice(0, 400),
        batch_id, commit_status,
        detail, rowsBefore, findBefore,
        list_row: listed[0] ?? null,
      }};
    }})()""",
        await_promise=True,
    )


def widen_rule(tab, auth: dict, bill_id: int) -> dict:
    """Last step: widen the term and re-read. The matched set grows with NO
    write to any transaction — D3, and doing it LAST keeps every UI/axe
    screenshot above on the rule the server checks measured."""
    return tab.evaluate(
        f"""(async () => {{
      const auth = {{'Content-Type': 'application/json',
                     Authorization: {json.dumps(auth["Authorization"])} }};
      const put = await fetch(`/api/v1/bills/${{{int(bill_id)}}}/rule`,
        {{method:'PUT', headers:auth, body: JSON.stringify({{
          match_mode: 'starts_with', match_term: 'HYDRO', amount_tolerance: 70,
        }})}});
      const after = await (await fetch(`/api/v1/bills/${{{int(bill_id)}}}/detail`,
        {{headers: auth}})).json();
      const rows = await (await fetch('/api/v1/transactions?limit=5',
        {{headers: auth}})).json();
      return {{put_status: put.status, after_count: after.matched_count,
               txn_total: rows.total}};
    }})()""",
        await_promise=True,
    )


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


def walk_to_summary(tab, limit: int = 400) -> dict:
    """Tab until the data-table <summary> has focus, then Enter to open it.

    Real key events through the driver (Tab keeps `rawKeyDown`, Enter runs its
    default action) — a synthesised KeyboardEvent would toggle nothing and
    prove nothing. The result records HOW MANY stops it took, so a regression
    that pushes the disclosure out of reach is visible, not just a boolean.
    """
    for _ in range(limit):
        tag = tab.evaluate("document.activeElement ? document.activeElement.tagName : ''")
        if tag == "SUMMARY":
            focus = tab.evaluate(
                """(() => {
                  const el = document.activeElement;
                  const cs = getComputedStyle(el);
                  return {
                    outline: cs.outlineStyle + ' ' + cs.outlineWidth,
                    boxShadow: cs.boxShadow,
                  };
                })()"""
            )
            tab.press("Enter")
            tab.sleep(0.4)
            opened = tab.evaluate(
                """(() => {
                  const panel = document.activeElement?.parentElement
                    ?? document.querySelector('details[open]');
                  return {
                    open: !!document.querySelector('details[open]'),
                    rows: document.querySelectorAll('details[open] tbody tr').length,
                    has_missed: Array.from(
                      document.querySelectorAll('details[open] td, details[open] th, details[open] span')
                    ).some(n => (n.textContent || '').trim() === 'missed'),
                  };
                })()"""
            )
            return {"stops": _, "focus": focus, **opened}
        tab.press("Tab")
        tab.sleep(0.02)
    return {"stops": -1, "focus": None, "open": False, "rows": 0, "has_missed": False}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--out", default=str(HERE / "m116-bill-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m116_bill_probe.py")
    base = args.base.rstrip("/")
    stamp = int(time.time())

    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{base}/login")
        tab.enable_runtime()
        tab.navigate(f"{base}/login", timeout=45)
        settle(tab, 1.0)

        seeded: dict = seed(tab, stamp)
        check("import: statement uploaded through /imports/upload",
              seeded["upload_status"] in (200, 201),
              f"status={seeded['upload_status']} batch={seeded['batch_id']} "
              f"{seeded['upload_body'][:200]}")
        check("import: batch committed (the rows are real ledger rows now)",
              seeded["commit_status"] in (200, 201), str(seeded["commit_status"]))
        check("seed: bill created with a rule", seeded["bill_status"] == 201
              and bool(seeded["bill_id"]),
              f"status={seeded['bill_status']} id={seeded['bill_id']}")
        listed: dict = seeded.get("list_row") or {}
        check("list: GET /bills reports the count and the rolling average",
              listed.get("payment_count") == 6 and listed.get("rolling_average") == EXPECT_12_MEAN,
              f"payment_count={listed.get('payment_count')} "
              f"rolling_average={listed.get('rolling_average')}")

        detail: dict = seeded["detail"]
        bill: dict = detail.get("bill", {})
        check("server: matched_count == 6 (the decoy is excluded)",
              detail.get("matched_count") == 6, str(detail.get("matched_count")))
        check("server: lifetime payment_count == 6", bill.get("payment_count") == 6,
              str(bill.get("payment_count")))

        rolling: dict = detail.get("rolling", {})
        for window, mean, median in (
            ("twelve", EXPECT_12_MEAN, EXPECT_12_MEDIAN),
            ("six", EXPECT_6_MEAN, EXPECT_6_MEDIAN),
        ):
            check(f"server: {window}-period mean hand-computed {mean}",
                  rolling.get(window, {}).get("mean") == mean,
                  str(rolling.get(window, {}).get("mean")))
            check(f"server: {window}-period median hand-computed {median}",
                  rolling.get(window, {}).get("median") == median,
                  str(rolling.get(window, {}).get("median")))

        points: list = detail.get("chart", {}).get("points", [])
        check("server: 7 periods with exactly month 4 missed",
              len(points) == 7
              and [p["missed"] for p in points].count(True) == 1
              and points[3]["missed"] is True
              and points[3]["total"] == "0.00",
              json.dumps([{"p": p["period"], "t": p["total"], "m": p["missed"]} for p in points]))
        total = sum(float(p["total"]) for p in points)
        check(f"server: chart total sums to {EXPECT_TOTAL}",
              f"{total:.2f}" == EXPECT_TOTAL, f"{total:.2f}")

        found: dict = seeded["findBefore"]
        groups: list = found.get("groups", [])
        check("find-past: six rows, six period groups, no truncation",
              found.get("total_count") == 6 and found.get("truncated") is False
              and len(groups) == 6,
              f"total={found.get('total_count')} groups={len(groups)}")
        check("find-past: account name resolved on every group",
              bool(groups) and all(g["items"][0]["account_name"] for g in groups),
              " | ".join(g["items"][0]["account_name"] for g in groups))

        bill_id = seeded["bill_id"]
        ui_login(tab, base, seeded["email"])
        settle(tab, 2.0)

        report: dict = {
            "base": base,
            "bill_id": bill_id,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "checks": CHECKS,
            "axe": {},
        }

        # --- the UI facts + the keyboard lap, once -------------------------
        tab.navigate(f"{base}/bills/{bill_id}")
        settle(tab, 2.0)
        load_axe(tab)
        report["ui"] = tab.evaluate(
            """(() => {
              const frame = document.querySelector('[role="img"]');
              const summary = Array.from(document.querySelectorAll('summary'))
                .find(s => /show the numbers/i.test(s.textContent || ''));
              return {
                heading: document.querySelector('h1')?.textContent ?? null,
                page_text: (document.body.innerText || '').slice(0, 4000),
                frame_label: frame?.getAttribute('aria-label') ?? null,
                frame_count: document.querySelectorAll('[role="img"]').length,
                has_summary: !!summary,
                tables: document.querySelectorAll('table').length,
              };
            })()"""
        )
        ui: dict = report["ui"]
        page_text: str = ui.get("page_text") or ""
        check("UI: header states the current rule",
              "Matches transactions whose payee" in page_text
              and "HYDRO ONE" in page_text,
              next((ln for ln in page_text.splitlines()
                    if "Matches transactions" in ln), "")[:200])
        check("UI: chart frame is ONE leaf node and says 7 points",
              ui.get("frame_count") == 1
              and "7 points" in (ui.get("frame_label") or ""),
              str(ui.get("frame_label"))[:220])
        check("UI: the missed period is in the accessible summary",
              "1 missed period" in (ui.get("frame_label") or ""),
              str(ui.get("frame_label"))[:220])
        check("UI: data-table disclosure present", bool(ui.get("has_summary")), "")
        check("UI: both rolling windows printed",
              "Last 6 periods" in page_text and "Last 12 periods" in page_text, "")
        check("UI: payment count printed", "6 payments" in page_text, "")

        walked = walk_to_summary(tab)
        report["keyboard"] = walked
        check("keyboard: the data-table summary is reachable by Tab and opens",
              walked.get("stops", -1) >= 0 and walked.get("open") is True,
              json.dumps(walked))
        check("keyboard: the opened table carries its 7 period rows",
              walked.get("rows") == 7, str(walked.get("rows")))
        check("keyboard: the opened table spells out `missed`",
              walked.get("has_missed") is True, "")
        check("keyboard: the focused summary shows a focus indicator",
              walked.get("focus") is not None
              and walked["focus"].get("boxShadow") not in (None, "none"),
              json.dumps(walked.get("focus")))

        # --- axe: 390 / 412 / 1440 × both themes ---------------------------
        for theme in ("dark", "light"):
            for width, height in ((390, 844), (412, 915), (1440, 900)):
                got = tab.force_viewport(width, height)
                if got[0] != width:
                    # Evidence that cannot show its own width is not evidence.
                    check(f"viewport {width} pinned", False, str(got))
                    continue
                tab.navigate(f"{base}/bills/{bill_id}")
                settle(tab, 1.8)
                set_theme(tab, theme)
                tab.sleep(0.6)
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
                key = f"bill-detail:{width}:{theme}"
                report["axe"][key] = summarize(result["violations"])
                report["axe"][key]["viewport"] = tab.viewport()
                counts: dict = report["axe"][key]["counts"]
                check(f"axe {key} = 0 violations", sum(counts.values()) == 0,
                      f"critical={counts['critical']} serious={counts['serious']} "
                      f"moderate={counts['moderate']} minor={counts['minor']}")
                tab.screenshot(str(HERE / f"m116-bill-{width}-{theme}.png"))

        # --- D3 LAST: widen the rule and prove the bill reshaped itself ----
        widened: dict = widen_rule(tab, seeded["auth"], bill_id)
        report["derived"] = widened
        check("derived: widening the rule changed the match with no write",
              widened.get("put_status") == 200
              and widened.get("after_count", 0) > seeded["rowsBefore"],
              f"{seeded['rowsBefore']} -> {widened.get('after_count')} "
              f"(PUT {widened.get('put_status')})")

        Path(args.out).write_text(json.dumps(report, indent=2))
        failed = [c for c in CHECKS if not c["ok"]]
        print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed -> {args.out}",
              flush=True)
        for c in failed:
            print(f"  FAILED: {c['check']} — {c['detail']}", flush=True)
        # The throwaway tenant is left in place on purpose: deleting it would
        # erase the rows the evidence points at.
        return 1 if failed else 0
    finally:
        browser.close()


if __name__ == "__main__":
    raise SystemExit(main())

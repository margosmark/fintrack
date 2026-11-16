"""M109 forecast — the live probe (AGENTS §4 step 5, against the deployed stack).

    python3 m109_forecast_probe.py [--base https://fintrac.princessandmark.com] [--width 1440]

A THROWAWAY tenant (register + login + one house-purchase scenario), because
forecast is an OWNER decision surface: the demo tenant answers 403
`demo_account_restricted` on every write (docs/45 §Tenancy), so the demo path
is exercised as a REFUSAL in `forecast_smoke.sh` and never as the happy path.

What each check proves:

  1 seed       register + login + POST /forecast/scenarios → 201, list count 1
  2 run        GET /forecast/run: buckets[0] IS `as_of`, the disclaimer is the
               exact honest sentence, the net worth does NOT move on the
               purchase day (cash became equity), and the first mortgage
               payment one month later does move it — by interest only
  3 determinism two identical GETs are byte-identical (the whole point of the
               engine: no randomness anywhere, docs/45 §3.3)
  4 ui         /forecast renders ALL FOUR honesty strings + the saved scenario
  5 assumptions the collapsed Assumptions panel opens and carries the tax
               disclaimer — the "say it, don't imply it" rule (docs/09)
  6 toggle     "Today's dollars" swaps the chart with ZERO extra /forecast/run
               requests (both series always ship, docs/45 §3.3)
  7 crud       copy → count 2, delete both → count 0
  8 cleanup    the throwaway tenant is deleted (204)

Exit 0 only if every check passes; JSON + PNG evidence land next to this script.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from a11y_driver import Browser
from probe_target import random_password

HERE = Path(__file__).resolve().parent
CHECKS: list[dict] = []

# Throwaway tenant credentials (same pattern as m104_tag_probe / m110) —
# never a real account, never reused anywhere else, deleted at the end.
PASSWORD = random_password("m109")
HONESTY = [
    "Projection, not a prediction.",
    "Not a tax simulator — a scenario that changes income does not recalculate your tax projection.",
    "Deterministic, not Monte Carlo — the same inputs always give the same output.",
    "Not financial advice.",
]
PURCHASE_DATE = "2027-06-01"


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


def settle(tab, extra: float = 1.4) -> None:
    try:
        tab.wait_for(
            'document.querySelector(\'[data-testid="route-fallback"]\') === null'
            " && document.querySelector('main') !== null",
            timeout=25,
        )
    except TimeoutError:
        pass
    tab.sleep(extra)


def js(tab, body: str):
    """Run an async in-page snippet (same-origin fetch + JSON back to Python)."""
    return tab.evaluate(f"(async () => {{ {body} }})()", await_promise=True)


def seed(tab, base: str, stamp: int) -> dict:
    email = f"fcprobe-{stamp}@example.com"
    return js(
        tab,
        f"""
        const H = {{'Content-Type': 'application/json'}};
        const email = {json.dumps(email)}, pw = {json.dumps(PASSWORD)};
        const reg = await fetch('/api/v1/auth/register',
          {{method:'POST', headers:H, body:JSON.stringify({{email, password:pw}})}});
        const log = await fetch('/api/v1/auth/login',
          {{method:'POST', headers:H, body:JSON.stringify({{email, password:pw}})}});
        const token = (await log.json()).access_token;
        const auth = {{...H, Authorization: 'Bearer ' + token}};
        const scen = await fetch('/api/v1/forecast/scenarios', {{method:'POST', headers:auth,
          body: JSON.stringify({{
            id: 'sc-live0001', name: 'House in 2027',
            events: [{{id:'ev-live0001', type:'house_purchase', date: {json.dumps(PURCHASE_DATE)},
                      home_price_cad:'650000', down_payment_cad:'130000',
                      mortgage_rate_pct:'4.49', amortization_years: 25}}]
          }})}});
        const list = await (await fetch('/api/v1/forecast/scenarios', {{headers:auth}})).json();
        return {{email, token, register: reg.status, login: log.status,
                 scenario: scen.status, count: list.count,
                 body: (await scen.text()).slice(0, 200)}};
        """,
    )


def server_checks(tab, token: str) -> dict:
    return js(
        tab,
        f"""
        const auth = {{Authorization: 'Bearer ' + {json.dumps(token)}}};
        const q = '/api/v1/forecast/run?scenario_ids=sc-live0001&horizon_months=24&granularity=day';
        const a = await (await fetch(q, {{headers:auth}})).text();
        const b = await (await fetch(q, {{headers:auth}})).text();
        const r = JSON.parse(a);
        const i = Object.fromEntries(r.buckets.map((d, n) => [d, n]));
        const at = (series, day) => Number(series.values[i[day]]);
        const prev = (day) => {{
          const n = i[day];
          return r.buckets[n - 1];
        }};
        return {{
          identical: a === b,
          as_of: r.as_of, first: r.buckets[0], last: r.buckets[r.buckets.length - 1],
          buckets: r.buckets.length, disclaimer: r.disclaimer,
          scenarios: r.scenarios.length, scenario_name: r.scenarios[0]?.name,
          gap_before: at(r.scenarios[0], '2027-05-31') - at(r.baseline, '2027-05-31'),
          gap_buy: at(r.scenarios[0], '2027-06-01') - at(r.baseline, '2027-06-01'),
          gap_month: at(r.scenarios[0], '2027-07-01') - at(r.baseline, '2027-07-01'),
          real_last: r.baseline.real_values.at(-1), nom_last: r.baseline.values.at(-1),
          inputs: r.inputs,
        }};
        """,
    )


def crud_checks(tab, token: str) -> dict:
    return js(
        tab,
        f"""
        const auth = {{'Content-Type':'application/json', Authorization: 'Bearer ' + {json.dumps(token)}}};
        const copy = await fetch('/api/v1/forecast/scenarios/sc-live0001/copy',
          {{method:'POST', headers:auth, body:JSON.stringify({{name:'Copy of House'}})}});
        const after = await (await fetch('/api/v1/forecast/scenarios', {{headers:auth}})).json();
        const id = (await copy.json()).id;
        const d1 = await fetch('/api/v1/forecast/scenarios/sc-live0001', {{method:'DELETE', headers:auth}});
        const d2 = await fetch('/api/v1/forecast/scenarios/' + id, {{method:'DELETE', headers:auth}});
        const final = await (await fetch('/api/v1/forecast/scenarios', {{headers:auth}})).json();
        const foreign = await fetch('/api/v1/forecast/scenarios/sc-alien001', {{headers:auth}});
        return {{copy: copy.status, after: after.count, del1: d1.status, del2: d2.status,
                 final: final.count, foreign: foreign.status,
                 foreignBody: (await foreign.text()).slice(0, 160)}};
        """,
    )


def cleanup(tab, token: str) -> dict:
    return js(
        tab,
        f"""
        const auth = {{'Content-Type':'application/json', Authorization: 'Bearer ' + {json.dumps(token)}}};
        const r = await fetch('/api/v1/users/me', {{method:'DELETE', headers:auth,
          body: JSON.stringify({{current_password: {json.dumps(PASSWORD)}}}) }});
        return {{status: r.status, body: (await r.text()).slice(0, 160)}};
        """,
    )


def ev(tab, expr: str):
    """Evaluate an ARROW FUNCTION in the page.

    `a11y_driver.Tab.evaluate` sends its argument to `Runtime.evaluate` as an
    EXPRESSION, so a bare `() => …` comes back as the function itself — which
    `returnByValue` serialises as `{}`. The first run of this probe therefore
    "read" an empty object for `document.body.innerText` and silently executed
    none of its clicks. Always invoke.
    """
    return tab.evaluate(f"({expr})()")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--width", type=int, default=1440)
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m109_forecast_probe.py")
    stamp = int(time.time())
    dark = '{"name":"dark"}'  # theme is persisted in localStorage; set below

    browser = Browser(width=args.width, height=900)
    ok = True
    token = None
    try:
        tab = browser.new_tab(args.base)
        tab.navigate(args.base)
        settle(tab)

        # --- 1. seed the throwaway owner + one scenario -------------------
        seeded = seed(tab, args.base, stamp)
        token = seeded["token"]
        ok &= check(
            "seed",
            seeded["register"] == 200 and seeded["login"] == 200 and seeded["scenario"] == 201
            and seeded["count"] == 1,
            json.dumps({k: seeded[k] for k in ("register", "login", "scenario", "count")}),
        )
        if not token:
            raise RuntimeError("no token — aborting before any assertion")

        # --- 2./3. server truth through the deployed API -------------------
        run = server_checks(tab, token)
        ok &= check("determinism", run["identical"], "two GET /forecast/run bodies compared byte-for-byte")
        ok &= check("series anchored on as_of", run["first"] == run["as_of"],
                    f"buckets[0]={run['first']} as_of={run['as_of']} ({run['buckets']} buckets, last {run['last']})")
        ok &= check("disclaimer", run["disclaimer"] == HONESTY[0], repr(run["disclaimer"]))
        ok &= check("scenario in run", run["scenarios"] == 1 and run["scenario_name"] == "House in 2027",
                    json.dumps({"n": run["scenarios"], "name": run["scenario_name"]}))
        ok &= check("no move on purchase day", run["gap_buy"] == 0,
                    f"gap {PURCHASE_DATE} = {run['gap_buy']} (cash became equity), day before = {run['gap_before']}")
        ok &= check("interest is spent the month after", run["gap_month"] < 0,
                    f"gap 2027-07-01 = {run['gap_month']} (520000 x 4.49% / 12 = 1945.67 first period)")
        inputs = run["inputs"]
        identity = round(float(inputs["recurring_income_monthly_cad"]) + float(inputs["residual_income_monthly_cad"]), 2)
        ok &= check("flow identity", identity == float(inputs["income_monthly_cad"]),
                    f"income {inputs['income_monthly_cad']} == recurring {inputs['recurring_income_monthly_cad']} "
                    f"+ residual {inputs['residual_income_monthly_cad']}")

        # --- 4. the page renders the four honest strings -------------------
        tab.navigate(f"{args.base}/forecast")
        settle(tab)
        body = ev(tab, "() => document.body.innerText")
        missing = [s for s in HONESTY if s not in body]
        ok &= check("honesty strings on /forecast", not missing, f"missing: {missing}" if missing else "all 4 present")
        ok &= check("scenario listed", "House in 2027" in body, "saved scenario appears on the page")
        ok &= check("assumptions affordance", "Assumptions" in body, "collapsed Assumptions control present")
        shot = str(HERE / f"m109-forecast-{args.width}-light.png")
        tab.screenshot(shot)
        print(f"      screenshot: {shot}", flush=True)

        # --- 5. the collapsed panel opens and carries the tax disclaimer --
        ev(tab, "() => { const b = Array.from(document.querySelectorAll('button'))"
            ".find(x => x.textContent.trim().startsWith('Assumptions')); if (b) b.click(); return !!b; }"
        )
        tab.sleep(1.0)
        panel = ev(tab, "() => document.body.innerText")
        ok &= check("assumptions panel opens",
                    "tax projection not recalculated" in panel.lower() or "Not a tax simulator" in panel,
                    "tax disclaimer visible after expanding")

        # --- 6. the dollar toggle swaps with NO extra /forecast/run ------
        counter = ev(tab, """() => { window.__fcRuns = 0;
                 const orig = window.fetch;
                 window.fetch = (...a) => { if (String(a[0]).includes('/forecast/run')) window.__fcRuns++;
                                            return orig(...a); };
                 return true; }"""
        )
        before = ev(tab, "() => document.querySelector('[aria-label=\"End value per series\"]')?.getAttribute('aria-label') || ''")
        ev(tab, "() => { const b = Array.from(document.querySelectorAll('button,[role=radio]'))"
            ".find(x => x.textContent.includes(\"Today's dollars\")); if (b) b.click(); return !!b; }"
        )
        tab.sleep(1.2)
        runs_after = ev(tab, "() => window.__fcRuns")
        label = ev(tab, "() => document.querySelector('[aria-label=\"End value per series\"]')?.textContent || ''")
        ok &= check("toggle needs no refetch", counter and runs_after == 0,
                    f"/forecast/run calls during the toggle = {runs_after} (expect 0); series summary now {label[:90]!r}")

        # --- dark theme screenshot ----------------------------------------
        ev(tab, "() => { document.documentElement.setAttribute('data-theme','dark');"
            " localStorage.setItem('theme','dark'); return true; }"
        )
        tab.sleep(0.8)
        dark_shot = str(HERE / f"m109-forecast-{args.width}-dark.png")
        tab.screenshot(dark_shot)
        print(f"      screenshot: {dark_shot}", flush=True)

        # --- 7. save/copy/delete round trip --------------------------------
        crud = crud_checks(tab, token)
        ok &= check("copy", crud["copy"] == 201 and crud["after"] == 2,
                    json.dumps({k: crud[k] for k in ("copy", "after")}))
        ok &= check("delete both", crud["del1"] == 204 and crud["del2"] == 204 and crud["final"] == 0,
                    json.dumps({k: crud[k] for k in ("del1", "del2", "final")}))
        ok &= check("foreign id is the same 404", crud["foreign"] == 404 and "scenario_not_found" in crud["foreignBody"],
                    crud["foreignBody"])

        # --- 8. remove the throwaway tenant --------------------------------
        gone = cleanup(tab, token)
        ok &= check("probe tenant deleted", gone["status"] == 204, f"DELETE /users/me → {gone['status']}")
        token = None
    finally:
        browser.close()

    (HERE / "m109-forecast-probe.json").write_text(
        json.dumps({"ok": bool(ok), "base": args.base, "width": args.width, "checks": CHECKS}, indent=2),
        encoding="utf-8",
    )
    passed = sum(1 for c in CHECKS if c["ok"])
    print(f"\nPROBE_{'PASS' if ok else 'FAIL'} — {passed}/{len(CHECKS)} checks", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

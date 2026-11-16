"""M117 — "refresh that refreshes" + the SSE indicator, live (AGENTS §4 step 5).

    python3 m117_refresh_probe.py [--base https://fintrac.princessandmark.com]
                                  [--width 390] [--height 844]

A THROWAWAY tenant (register + login + one account + one security + one
committed trade), because the demo tenant answers 403 on every write. The
security is HELD, so it lands in `/portfolio/refresh`'s fan-out and in the
market-hours `GET /portfolio/positions` fan-out.

What each check proves:

  1 seed    `POST /securities` queues the 365-day backfill and the trade makes
            the security held
  2 stale   `DELETE /prices/{today}` → `GET /portfolio/positions` reports
            `price_age_days == 1` — the state that used to be PERMANENT: the
            `PRICE_STALE_DAYS = 7` freshness window was being used as the fetch
            gate, so a 1-day-old close was handed straight back and neither the
            button nor the auto-refresh could advance it
  3 auto    the next `GET /portfolio/positions` during market hours returns
            `price_age_days == 0` with NO manual action (acceptance: positions
            show today's price without a per-security refresh)
  4 button  delete today's row again, `POST /portfolio/refresh`, poll → `age 0`
            within 15 s (acceptance: RefreshPricesButton → rows update ≤15s)
  5 SSE     after a real form login, `/investments` renders
            `Stream connected` — not the amber `prices stale` that M65 shows
            whenever any row is >24h old, which is what "no streaming" looked
            like — and the `Last stream message:` title CHANGES over 7 s, which
            proves events are actually arriving rather than a one-shot open
  6 axe     0 critical + 0 serious on `/investments` in both themes
  7 shots   screenshots of `/investments` at this width × theme

Exit 0 only if every check passes; JSON + PNG evidence land next to this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date as _date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from a11y_driver import Browser  # noqa: E402
from axe_scan import load_axe, run_axe, set_theme, summarize  # noqa: E402
from probe_target import random_password

CHECKS: list[dict] = []

# Throwaway tenant credentials (same pattern as the m104/m110/m116 probes) —
# never a real account, never reused anywhere else.
PASSWORD = random_password("m117")
SYMBOL = "MSFT"
BUDGET_SECONDS = 15  # acceptance: rows update within 15 s


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


def seed(tab, stamp: int) -> dict:
    """Register, sign in, create an account, a security and a held trade — then
    delete today's close so the tenant starts in the stale state."""
    email = f"m117-probe-{stamp}@example.com"
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
          name:'M117 probe', type:'tfsa', currency:'CAD'}})}});
      const account_id = (await acct.json()).id;

      const sec = await fetch('/api/v1/securities',
        {{method:'POST', headers:auth, body: JSON.stringify({{
          symbol:{json.dumps(SYMBOL)}, name:'Microsoft', kind:'stock',
          listing_currency:'USD', exchange:'NMS'}})}});
      const sec_json = await sec.json();
      const security_id = sec_json.id ?? null;

      const today = new Date().toISOString().slice(0, 10);
      const trade = await fetch('/api/v1/trades',
        {{method:'POST', headers:auth, body: JSON.stringify({{
          account_id, security_id, action:'buy', date: today,
          quantity:'1', price_native:'100'}})}});

      // The backfill runs as a BackgroundTask AFTER the 201 response, so wait
      // for it to land before touching the rows it writes.
      let rows = {{}};
      for (let i = 0; i < 30; i++) {{
        const list = await (await fetch(
          `/api/v1/prices?security_id=${{security_id}}&limit=200`, {{headers:auth}})).json();
        rows = {{}};
        for (const r of (list.items ?? [])) rows[r.date] = r.id;
        if (rows[today] !== undefined) break;
        await new Promise(r => setTimeout(r, 1000));
      }}

      const positions_after_seed = await (await fetch(
        '/api/v1/portfolio/positions', {{headers:auth}})).json();

      return {{
        email, auth, account_id, security_id, today,
        sec_status: sec.status, sec_body: JSON.stringify(sec_json).slice(0, 200),
        trade_status: trade.status, trade_body: (await trade.text()).slice(0, 200),
        price_ids: rows,
        positions_after_seed,
      }};
    }})()""",
        await_promise=True,
    )


def age_days_of(row: dict) -> int | None:
    """`(today - row.date)` for one `/prices` item — the same day-granular age
    `positions.price_age_days` reports, read without the side effect that
    `GET /portfolio/positions` has (it schedules its own background refresh)."""
    if not row or not row.get("date"):
        return None
    return (_date.today() - _date.fromisoformat(row["date"])).days


def age_of(payload: dict) -> object:
    items = (payload or {}).get("items") or []
    if not items:
        return None
    return items[0].get("price_age_days")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--out", default=str(HERE / "m117-refresh-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m117_refresh_probe.py")
    base = args.base.rstrip("/")
    stamp = int(time.time())

    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{base}/login")
        tab.enable_runtime()
        tab.navigate(f"{base}/login", timeout=45)
        settle(tab, 1.0)
        tab.force_viewport(args.width, args.height)

        seeded: dict = seed(tab, stamp)
        auth_js = seeded["auth"]
        security_id = seeded["security_id"]
        today = seeded["today"]

        check("seed: security created", seeded["sec_status"] in (200, 201) and bool(security_id),
              f"status={seeded['sec_status']} id={security_id} {seeded['sec_body']}")
        check("seed: trade accepted (security is HELD, so it is in the fan-out)",
              seeded["trade_status"] == 201, str(seeded["trade_status"]))

        # --- 2: the stale state, which the old freshness window made permanent
        pos_seed = seeded["positions_after_seed"]
        check("seed: positions report today's close (age 0)",
              age_of(pos_seed) == 0, f"age={age_of(pos_seed)} total={pos_seed.get('total')}")

        today_price_id = seeded["price_ids"].get(today)
        check("seed: a price row dated today exists to delete",
              today_price_id is not None, f"price_id={today_price_id}")

        # Let the background task the seed's positions GET scheduled finish
        # first — otherwise it could re-insert the row we are about to delete.
        tab.sleep(2.0)

        deleted = tab.evaluate(
            f"""(async () => {{
              const r = await fetch('/api/v1/prices/{today_price_id}',
                {{method:'DELETE', headers:{auth_js}}});
              return r.status;
            }})()""",
            await_promise=True,
        )
        check("stale: today's close deleted (age must now be 1)",
              deleted == 204, f"status={deleted}")

        stale = tab.evaluate(
            """(async () => {
                const r = await fetch('/api/v1/portfolio/positions',
                  {headers: AUTH}); return await r.json();
            })()""".replace("AUTH", json.dumps(auth_js)),
            await_promise=True,
        )
        check("stale: GET /portfolio/positions reports price_age_days == 1",
              age_of(stale) == 1, f"age={age_of(stale)}")

        # --- 3: the market-hours auto-refresh must heal it with no user action
        healed_age = None
        auto_elapsed = None
        t0 = time.time()
        while time.time() - t0 < 20:
            tab.sleep(1.5)
            again = tab.evaluate(
                """(async () => {
                    const r = await fetch('/api/v1/portfolio/positions',
                      {headers: AUTH}); return await r.json();
                })()""".replace("AUTH", json.dumps(auth_js)),
                await_promise=True,
            )
            healed_age = age_of(again)
            if healed_age == 0:
                auto_elapsed = round(time.time() - t0, 1)
                break
        check("auto: positions reach age 0 with no manual refresh",
              healed_age == 0, f"age={healed_age} after={auto_elapsed}s")

        # --- 4: the button contract, measured the way the acceptance words it
        # Re-read the id first: the auto-refresh above re-inserted today's row
        # under a NEW id, so deleting the seed's id would 404 and leave the
        # precondition at age 0 (measured on the first run of this probe).
        second_delete = tab.evaluate(
            f"""(async () => {{
              const list = await (await fetch(
                '/api/v1/prices?security_id={security_id}&limit=200',
                {{headers:{auth_js}}})).json();
              const row = (list.items ?? []).find(r => r.date === {json.dumps(today)});
              if (!row) return JSON.stringify({{status: null, why: 'no row for today'}});
              const r = await fetch(`/api/v1/prices/${{row.id}}`,
                {{method:'DELETE', headers:{auth_js}}});
              return JSON.stringify({{status: r.status, id: row.id}});
            }})()""",
            await_promise=True,
        )
        print(f"       second delete -> {second_delete}", flush=True)
        check("button: today's close re-deleted (fresh row id after the auto heal)",
              json.loads(str(second_delete)).get("status") == 204, str(second_delete))
        # The precondition and the progress are read from `/prices`, never from
        # `/portfolio/positions`: every positions GET schedules its own
        # market-hours background refresh, which heals the row behind the
        # button's back and makes the button's own contribution unmeasurable
        # (measured on the first run — the auto path re-inserted the close
        # before the POST landed).
        def newest_close() -> dict:
            payload = tab.evaluate(
                """(async () => {
                    const r = await fetch('/api/v1/prices?security_id=SID&limit=1',
                      {headers: AUTH}); return await r.json();
                })()""".replace("SID", str(security_id))
                        .replace("AUTH", json.dumps(auth_js)),
                await_promise=True,
            )
            items = (payload or {}).get("items") or []
            return items[0] if items else {}

        pre = newest_close()
        check("button: pre-condition age == 1 (side-effect free read)",
              age_days_of(pre) == 1, f"newest={pre}")

        queued = tab.evaluate(
            """(async () => {
                const r = await fetch('/api/v1/portfolio/refresh',
                  {method:'POST', headers: AUTH});
                return JSON.stringify({status: r.status, body: await r.json()});
            })()""".replace("AUTH", json.dumps(auth_js)),
            await_promise=True,
        )
        queued_json = json.loads(queued)
        check("button: POST /portfolio/refresh answers 202 with queued >= 1",
              queued_json.get("status") == 202 and (queued_json.get("body") or {}).get("queued", 0) >= 1,
              queued)

        btn_age, btn_elapsed = None, None
        t1 = time.time()
        while time.time() - t1 < BUDGET_SECONDS:
            tab.sleep(1.0)
            btn_age = age_days_of(newest_close())
            if btn_age == 0:
                btn_elapsed = round(time.time() - t1, 1)
                break
        check(f"button: today's close is back within {BUDGET_SECONDS}s (no other "
              "endpoint polled in between)",
              btn_age == 0 and (btn_elapsed or 999) <= BUDGET_SECONDS,
              f"age={btn_age} elapsed={btn_elapsed}s")

        # ...and the acceptance's observable: what /investments actually shows.
        positions_after = tab.evaluate(
            """(async () => {
                const r = await fetch('/api/v1/portfolio/positions',
                  {headers: AUTH}); return await r.json();
            })()""".replace("AUTH", json.dumps(auth_js)),
            await_promise=True,
        )
        check("acceptance: GET /portfolio/positions reports price_age_days == 0",
              age_of(positions_after) == 0, f"age={age_of(positions_after)}")

        # --- 5/6/7: the real browser — indicator, axe, screenshots
        ui_login(tab, base, seeded["email"])
        settle(tab, 2.0)

        report: dict = {
            "base": base,
            "width": args.width,
            "security_id": security_id,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "checks": CHECKS,
            "auto_elapsed_s": auto_elapsed,
            "button_elapsed_s": btn_elapsed,
            "axe": {},
            "shots": [],
        }

        tab.navigate(f"{base}/investments")
        settle(tab, 2.5)

        first = indicator_state(tab)
        check("sse: indicator reaches 'Stream connected'",
              first.get("label") == "Stream connected",
              json.dumps(first))
        check("sse: a stream message has been received",
              bool(first.get("title")) and "No stream message" not in (first.get("title") or ""),
              str(first.get("title")))
        check("ui: Refresh prices button rendered",
              tab.evaluate("!!document.querySelector('[data-testid=\"refresh-prices\"]')"))

        tab.sleep(7.0)
        second = indicator_state(tab)
        check("sse: title CHANGES over 7s (events are arriving, not a one-shot open)",
              second.get("title") != first.get("title"),
              f"{first.get('title')} -> {second.get('title')}")

        load_axe(tab)
        for theme in ("dark", "light"):
            set_theme(tab, theme)
            tab.sleep(0.8)
            result = run_axe(tab)
            report["axe"][theme] = summarize(result["violations"])
            c = report["axe"][theme]["counts"]
            print(f"axe {theme:5s} critical={c['critical']} serious={c['serious']} "
                  f"moderate={c['moderate']} minor={c['minor']}", flush=True)
            shot = str(HERE / f"m117-investments-{args.width}-{theme}.png")
            tab.screenshot(shot)
            report["shots"].append(shot)

        bad = sum(
            v["counts"]["critical"] + v["counts"]["serious"] for v in report["axe"].values()
        )
        check("axe: 0 critical + 0 serious on /investments in both themes", bad == 0, str(bad))
        report["console"] = tab.console_errors()
        check("console: no errors on /investments", not report["console"],
              json.dumps(report["console"])[:400])
    finally:
        browser.close()

    report["checks"] = CHECKS
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    failed = [c for c in CHECKS if not c["ok"]]
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


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


def indicator_state(tab) -> dict:
    """The SSE indicator's label and `title` (its last-message timestamp).

    The component is the only `[title*="stream message"]` node on the page, so
    this reads the same two React states a human would: the rendered label and
    the tooltip clock.
    """
    return tab.evaluate(
        """(() => {
          const node = Array.from(document.querySelectorAll('[title]'))
            .find(n => /stream message/i.test(n.getAttribute('title') || ''));
          if (!node) return {label: null, title: null, found: false};
          const span = node.querySelector('span');
          return {
            found: true,
            label: (span && span.textContent || '').trim(),
            title: node.getAttribute('title'),
            dot: !!node.querySelector('div'),
          };
        })()"""
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""M122 bill-rule UX — the live probe (AGENTS §4 step 5, deployed stack).

    python3 m122_bill_ux_probe.py [--base https://fintrac.princessandmark.com]

A THROWAWAY tenant (register + login + one account + one CSV import), because
the demo tenant answers 403 `demo_account_restricted` on every write. What
each check proves, in the order the card's acceptance states it:

  1 seed      statement rows — four `ROGERS` (Feb/Mar/May/Jun), one
              `rogers #77` (Jul — same payee KEY after normalization, newest
              spelling), two `NETFLIX` (Apr/May), one `COFFEE BEAN` (Jun) and
              a decoy `FIDO` — imported through the REAL pipeline; bills
              "Rogers" (`["ROGERS"]`) and "Netflix" (`["NETFLIX"]`).
  2 suggest   `GET /bills/suggest-terms`: the ROGERS group is ONE group of 5
              labelled by its newest spelling, ranked first by frequency;
              `q=rog` narrows to it; a digits-only `q` answers `[]`; with
              `bill_id` the ROGERS group (the saved rule's matched set) is
              EXCLUDED while NETFLIX stays.
  3 stats     `detail.stats`: all-time 255.00 / 5 / 51.00 / yearly 255.00,
              YTD 255.00 (all rows are calendar-2026), and the list rows'
              `ytd_total` per bill.
  4 line      the chart's `rolling_six` over totals
              [50, 50, 0 (missed Apr), 50, 50, 55] hand-recomputed to
              [50.00, 50.00, 33.33, 37.50, 40.00, 42.50], last == `six.mean`.
  5 preview   `POST /bills/find-past` with an UNSAVED `["NETFLIX"]` rule —
              the T2 dry-run: 2 matches, first/last Apr→May, last-12 total
              31.98, per-account count 2.
  6 UI        `/bills/:id` -> Edit rule: the quick-add row offers the
              ledger's payees WITHOUT the bill's own group; a ≥3-char draft
              opens the listbox (`aria-expanded`, `aria-activedescendant`),
              ArrowDown + Enter adds the SUGGESTION; Test renders the
              "Draft preview — not saved" panel; the Spending stats card and
              the fallback table's "6-period avg" column are on the page.
  7 axe       0 violations at 390, 412 and 1440 in BOTH themes, with the
              rule editor OPEN (and the suggestion listbox open in one run).
  8 shots     screenshots of every width x theme.

Exit 0 only if every check passes; JSON + PNG evidence land next to this
script. Fixture dates are ABSOLUTE (calendar 2026, all ≤ probe day) — the
probe refuses to run in a later year rather than silently re-dating them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from a11y_driver import Browser
from axe_scan import load_axe, run_axe, set_theme, summarize
from probe_target import random_password

CHECKS: list[dict] = []

# Throwaway tenant credentials (same pattern as the m104/m116/m121 probes).
PASSWORD = random_password("m122")
# (payee, month, amount) — April is deliberately EMPTY for ROGERS: the missed
# period the rolling line must dip through. The newest spelling is a SAME-KEY
# variant (`rogers #77` -> `ROGERS` after the shared normalizer strips the
# trailing `#77`); `ROGERS COMMUNICATIONS` would be a DIFFERENT key — only a
# term containing "ROGERS" reaches it, which is exactly why the group must
# not collapse with it.
ROGERS_ROWS = [("ROGERS", 2, 50), ("ROGERS", 3, 50), ("ROGERS", 5, 50), ("ROGERS", 6, 50)]
NEWEST_SPELLING = ("rogers #77", 7, 55)  # same KEY as the four above
NETFLIX_ROWS = [("NETFLIX", 4, 15.99), ("NETFLIX", 5, 15.99)]
OTHER_ROWS = [("COFFEE BEAN", 6, 4.50), ("FIDO", 6, 75)]
YEAR = 2026

# Hand-computed expectations — literals, so a refactor that counts the wrong
# population fails here (the M91 standard).
ROGERS_GROUP_COUNT = 5  # four COMMUNICATIONS + one rogers #77
ROGERS_GROUP_TOTAL = "255.00"  # 4 x 50 + 55
NETFLIX_GROUP_COUNT = 2
ALL_TIME = {"total": "255.00", "payments": 5, "average": "51.00",
            "years": 1, "yearly_average": "255.00"}
YTD = {"year": YEAR, "total": "255.00", "payments": 5}
# buckets Feb..Jul with Apr missed: totals [50, 50, 0, 50, 50, 55]
ROLLING_LINE = ["50.00", "50.00", "33.33", "37.50", "40.00", "42.50"]
ROLLING_MEAN_6 = "42.50"  # 255 / 6
NETFLIX_LAST12 = "31.98"  # 2 x 15.99


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


# ---------------------------------------------------------------------------
# 1. seed — register, import through the REAL pipeline, create both bills
# ---------------------------------------------------------------------------

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
    {method:'POST', headers:auth, body: JSON.stringify({name:'M122 probe', type:'chequing', currency:'CAD'})});
  const account_id = (await acct.json()).id;

  const form = new FormData();
  form.append('file', new Blob([__CSV__], {type:'text/csv'}), 'm122.csv');
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

  const mk = async (name, terms) => {
    const r = await fetch('/api/v1/bills', {method:'POST', headers:auth,
      body: JSON.stringify({name, frequency: 'monthly', next_due_date: '__YEAR__-09-05',
        match_mode: 'contains', match_terms: terms, match_logic: 'any'})});
    return {status: r.status, body: await r.json()};
  };
  const rogers = await mk('Rogers', ['ROGERS']);
  const netflix = await mk('Netflix', ['NETFLIX']);

  const detail = await (await fetch('/api/v1/bills/' + rogers.body.id + '/detail',
    {headers: auth})).json();
  const listed = await (await fetch('/api/v1/bills', {headers: auth})).json();
  const bare = await (await fetch('/api/v1/bills/suggest-terms', {headers: auth})).json();
  const scoped = await (await fetch('/api/v1/bills/suggest-terms?bill_id=' + rogers.body.id,
    {headers: auth})).json();
  const filtered = await (await fetch('/api/v1/bills/suggest-terms?q=rog',
    {headers: auth})).json();
  const digits = await (await fetch('/api/v1/bills/suggest-terms?q=123',
    {headers: auth})).json();
  const draft = await fetch('/api/v1/bills/find-past', {method:'POST', headers:auth,
    body: JSON.stringify({match_mode:'contains', match_terms:['NETFLIX'],
                          match_logic:'any', frequency:'monthly'})});

  return {
    email, account_id, token,
    upload_status: up.status, batch_id, commit_status,
    rogers: rogers, netflix: netflix,
    detail, list: listed,
    suggest_bare: bare, suggest_scoped: scoped,
    suggest_filtered: filtered, suggest_digits: digits,
    draft_preview: await draft.json(), draft_status: draft.status,
  };
})()
"""


def seed(tab, stamp: int) -> dict:
    """Register, sign in, IMPORT the statement (docs/03), create the bills.

    The rows come through `POST /imports/upload` + `/commit` — the real
    import flow, not hand-posted transactions — so every number below is
    proven against the pipeline that actually produces a ledger.
    """
    csv_rows = ["Date,Description,Amount"]
    for payee, month, amount in [*ROGERS_ROWS, NEWEST_SPELLING, *NETFLIX_ROWS, *OTHER_ROWS]:
        csv_rows.append(f"{YEAR}-{month:02d}-05,{payee},-{amount:.2f}")
    csv_body = "\n".join(csv_rows) + "\n"
    email = f"m122-probe-{stamp}@example.com"
    js = (
        SEED_JS.replace("__EMAIL__", json.dumps(email))
        .replace("__PASSWORD__", json.dumps(PASSWORD))
        .replace("__CSV__", json.dumps(csv_body))
        .replace("__YEAR__", str(YEAR))
    )
    return tab.evaluate(js, await_promise=True)


def ui_login(tab, base: str, email: str) -> None:
    """Sign in THROUGH THE FORM — the access token lives in memory only
    (frontend/src/api/client.ts), so a token minted in `seed()` is invisible
    to the SPA. Same credentials, same tenant, one real session."""
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


def page_state(tab) -> dict:
    """The open editor + the page's M122 surfaces, read from the DOM."""
    return tab.evaluate(
        """(() => {
      const quickAddLabel = Array.from(document.querySelectorAll('span'))
        .find(s => (s.textContent || '').trim() === 'In your ledger:');
      const quickRow = quickAddLabel ? quickAddLabel.parentElement : null;
      const quick = quickRow ? Array.from(quickRow.querySelectorAll('button'))
        .map(b => (b.getAttribute('aria-label') || '')) : [];
      const statsCard = Array.from(document.querySelectorAll('h2,h3,div'))
        .map(n => (n.textContent || '').trim())
        .filter(t => t === 'Spending stats').length > 0;
      const body = document.body.innerText || '';
      const table = Array.from(document.querySelectorAll('details table'))
        .map(t => Array.from(t.querySelectorAll('th')).map(th => (th.textContent || '').trim()));
      return {
        quick_add: quick,
        spending_stats_card: statsCard,
        has_all_time: body.includes('All-time total'),
        has_this_year: body.includes('This year ('),
        has_yearly_average: body.includes('Yearly average'),
        fallback_headers: table.length ? table[0] : [],
        test_button: Array.from(document.querySelectorAll('button'))
          .some(b => (b.textContent || '').trim() === 'Test'),
        page_text: body.slice(0, 4000),
      };
    })()"""
    )


SUGGEST_UI_JS = """
(async () => {
  const input = Array.from(document.querySelectorAll('input'))
    .find(i => (document.querySelector('label[for="' + i.id + '"]')?.textContent || '')
               .trim().startsWith('Match terms'));
  if (!input) return {error: 'no term input'};
  const label = document.querySelector('label[for="' + input.id + '"]');
  const listboxId = () => input.getAttribute('aria-controls');
  const options = () => {
    const id = listboxId();
    const list = id ? document.getElementById(id) : null;
    return list ? Array.from(list.querySelectorAll('[role="option"]'))
      .map(o => ({id: o.id, text: (o.textContent || '').trim(),
                  selected: o.getAttribute('aria-selected')})) : [];
  };
  const chips = () => Array.from(
    document.querySelectorAll('ul[aria-label="Match terms"] li'))
    .map(li => (li.firstChild?.textContent || '').trim());
  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  const set = (v) => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, v);
    input.dispatchEvent(new Event('input', {bubbles: true}));
  };
  const fire = (key) => input.dispatchEvent(
    new KeyboardEvent('keydown', {key, bubbles: true, cancelable: true}));

  const before = chips();
  set('cof');
  await wait(1200);  // 200ms debounce + the round trip
  const expanded = input.getAttribute('role') === 'combobox'
    && input.getAttribute('aria-expanded') === 'true';
  const shown = options();
  const active0 = input.getAttribute('aria-activedescendant');
  fire('ArrowDown');
  await wait(120);
  const active1 = input.getAttribute('aria-activedescendant');
  const selected = options().filter(o => o.selected === 'true')
    .map(o => o.text);
  fire('Enter');
  await wait(200);
  const after = chips();
  const cleared = input.value;

  // The Test action, with the suggestion chip now part of the draft.
  const testBtn = Array.from(document.querySelectorAll('button'))
    .find(b => (b.textContent || '').trim() === 'Test');
  if (testBtn) testBtn.click();
  await wait(1500);
  const panel = Array.from(document.querySelectorAll('[aria-label="Draft preview — not saved"], span'))
    .map(n => (n.textContent || '').trim())
    .find(t => t === 'Draft preview — not saved') ?? null;
  const body = document.body.innerText || '';
  return {
    input_role: input.getAttribute('role'),
    expanded, options: shown, active_before: active0, active_after: active1,
    selected_on_enter: selected, chips_before: before, chips_after: after,
    draft_cleared: cleared === '',
    test_clicked: Boolean(testBtn),
    panel_visible: panel !== null,
    matching_line: body.split('\\n').find(l => l.includes('matching transaction')) ?? null,
    first_last: body.split('\\n').find(l => l.includes('→')) ?? null,
    page_text: body.slice(0, 4000),
  };
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--out", default=str(HERE / "m122-bill-ux-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m122_bill_ux_probe.py")
    base = args.base.rstrip("/")
    stamp = int(time.time())
    report: dict = {"checks": CHECKS, "axe": {}}

    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{base}/login")
        tab.enable_runtime()
        tab.navigate(f"{base}/login", timeout=45)
        settle(tab, 1.0)

        check(
            "fixture: the probe year matches the ABSOLUTE 2026 dates",
            datetime.now(UTC).year == YEAR,
            f"run year {datetime.now(UTC).year} vs fixture {YEAR}",
        )

        # ---- 1. seed -------------------------------------------------------
        seeded: dict = seed(tab, stamp)
        check(
            "seed: statement uploaded through /imports/upload and committed",
            seeded["upload_status"] in (200, 201) and seeded["commit_status"] in (200, 201),
            f"upload={seeded['upload_status']} commit={seeded['commit_status']} "
            f"batch={seeded['batch_id']}",
        )
        rogers_id = (seeded["rogers"] or {}).get("body", {}).get("id")
        check(
            "seed: both bills created (Rogers + Netflix)",
            (seeded["rogers"] or {}).get("status") == 201
            and (seeded["netflix"] or {}).get("status") == 201
            and bool(rogers_id),
            f"rogers={seeded['rogers'].get('status')} netflix={seeded['netflix'].get('status')}",
        )

        # ---- 2. suggest-terms ---------------------------------------------
        bare: list = seeded["suggest_bare"]
        report["suggest_bare"] = bare
        check(
            "suggest: the two spellings are ONE group, labelled by its newest row",
            bare[0]["normalized_payee"] == NEWEST_SPELLING[0]
            and bare[0]["count"] == ROGERS_GROUP_COUNT
            and bare[0]["total"] == ROGERS_GROUP_TOTAL,
            json.dumps(bare[:3]),
        )
        names = [item["normalized_payee"] for item in bare]
        check(
            "suggest: ranked by frequency (ROGERS 5 > NETFLIX 2 > the 1-count payees)",
            names.index("NETFLIX") == 1 and len(bare) >= 3,
            json.dumps(names),
        )
        filtered = seeded["suggest_filtered"]
        check(
            "suggest: q=rog narrows to the ROGERS group only",
            [i["normalized_payee"] for i in filtered] == [NEWEST_SPELLING[0]],
            json.dumps(filtered),
        )
        check(
            "suggest: a digits-only draft answers [] (never a silent match-all)",
            seeded["suggest_digits"] == [],
            json.dumps(seeded["suggest_digits"]),
        )
        scoped: list = seeded["suggest_scoped"]
        scoped_names = [i["normalized_payee"] for i in scoped]
        check(
            "suggest: with bill_id the SAVED rule's own payees are excluded",
            NEWEST_SPELLING[0] not in scoped_names and "NETFLIX" in scoped_names,
            json.dumps(scoped_names),
        )

        # ---- 3. all-time + YTD stats ---------------------------------------
        detail: dict = seeded["detail"]
        report["detail_stats"] = detail.get("stats")
        check(
            "stats: all-time hand-recomputed (255.00 / 5 / 51.00 / yearly 255.00)",
            detail.get("stats", {}).get("all_time") == ALL_TIME,
            json.dumps(detail.get("stats", {}).get("all_time")),
        )
        check(
            "stats: YTD is the CALENDAR year over the same set",
            detail.get("stats", {}).get("ytd") == YTD,
            json.dumps(detail.get("stats", {}).get("ytd")),
        )
        listed: list = seeded["list"]
        ytd_by_name = {row["name"]: row.get("ytd_total") for row in listed}
        check(
            "stats: the LIST rows carry ytd_total per bill",
            ytd_by_name.get("Rogers") == "255.00" and ytd_by_name.get("Netflix") == "31.98",
            json.dumps(ytd_by_name),
        )

        # ---- 4. the rolling line -------------------------------------------
        points = detail.get("chart", {}).get("points", [])
        line = [p.get("rolling_six") for p in points]
        report["rolling_line"] = line
        check(
            "line: trailing means hand-recomputed through the missed April",
            line == ROLLING_LINE and points[2]["missed"] is True,
            f"line={line} missed={[p['missed'] for p in points]}",
        )
        check(
            "line: the LAST point IS the card's rolling.six.mean (42.50)",
            line[-1] == detail.get("rolling", {}).get("six", {}).get("mean") == ROLLING_MEAN_6,
            f"line[-1]={line[-1]} six.mean={detail.get('rolling', {}).get('six', {})}",
        )

        # ---- 5. the unsaved dry-run ----------------------------------------
        draft: dict = seeded["draft_preview"]
        report["draft_preview"] = draft
        stats = draft.get("stats", {})
        check(
            "preview: an UNSAVED draft rule answers its own summary (T2)",
            seeded["draft_status"] == 200
            and draft.get("total_count") == 2
            and stats.get("first_date") == f"{YEAR}-04-05"
            and stats.get("last_date") == f"{YEAR}-05-05"
            and stats.get("last_12_total") == NETFLIX_LAST12
            and (stats.get("account_counts") or [{}])[0].get("count") == 2,
            json.dumps(stats),
        )

        # ---- 6. the UI ------------------------------------------------------
        token = seeded["token"]
        ui_login(tab, base, seeded["email"])
        settle(tab, 2.0)
        tab.navigate(f"{base}/bills/{rogers_id}")
        settle(tab, 2.0)

        opened: dict = tab.evaluate(OPEN_EDITOR_JS)
        check("UI: Edit rule opened", opened.get("opened") is True, str(opened))
        settle(tab, 1.6)
        state: dict = page_state(tab)
        report["ui"] = state
        check(
            "UI: quick-add chips come from the ledger, WITHOUT the bill's own payee",
            any("NETFLIX" in label and "×2" in label for label in state["quick_add"])
            and not any("rogers" in label.lower() for label in state["quick_add"]),
            json.dumps(state["quick_add"]),
        )
        check(
            "UI: the Spending stats card shows all-time / this-year / yearly",
            state["spending_stats_card"]
            and state["has_all_time"]
            and state["has_this_year"]
            and state["has_yearly_average"],
            json.dumps({k: state[k] for k in
                        ("spending_stats_card", "has_all_time", "has_this_year",
                         "has_yearly_average")}),
        )
        check(
            "UI: the chart fallback table carries the 6-period avg column",
            "6-period avg" in state["fallback_headers"],
            json.dumps(state["fallback_headers"]),
        )
        check("UI: a Test action sits in the editor", state["test_button"], "")

        lap: dict = tab.evaluate(SUGGEST_UI_JS, await_promise=True)
        report["ui_lap"] = lap
        check(
            "UI: the term input is a combobox that EXPANDS into the listbox",
            lap.get("input_role") == "combobox"
            and lap.get("expanded") is True
            and any("COFFEE BEAN" in (o.get("text") or "") for o in lap.get("options", [])),
            json.dumps({k: lap.get(k) for k in ("input_role", "expanded", "options")}),
        )
        check(
            "UI: ArrowDown sets aria-activedescendant to a real option",
            bool(lap.get("active_after"))
            and lap.get("active_after") != lap.get("active_before"),
            f"before={lap.get('active_before')} after={lap.get('active_after')}",
        )
        check(
            "UI: Enter adds the SUGGESTION as a chip and clears the draft",
            lap.get("draft_cleared") is True
            and any("COFFEE BEAN" in chip for chip in lap.get("chips_after", []))
            and "COFFEE BEAN" not in lap.get("chips_before", []),
            json.dumps({k: lap.get(k) for k in ("chips_before", "chips_after", "draft_cleared")}),
        )
        check(
            "UI: Test renders the 'Draft preview — not saved' panel with its count",
            lap.get("test_clicked") is True
            and lap.get("panel_visible") is True
            and lap.get("matching_line") is not None,
            json.dumps({k: lap.get(k) for k in ("test_clicked", "panel_visible", "matching_line")}),
        )

        # ---- 7/8. axe + screenshots (editor OPEN; one run with the LISTBOX) -
        for theme in ("dark", "light"):
            for width, height in ((390, 844), (412, 915), (1440, 900)):
                got = tab.force_viewport(width, height)
                if got[0] != width:
                    check(f"viewport {width} pinned", False, str(got))
                    continue
                tab.navigate(f"{base}/bills/{rogers_id}")
                settle(tab, 1.6)
                opened = tab.evaluate(OPEN_EDITOR_JS)
                settle(tab, 1.0)
                if width == 390 and theme == "dark":
                    # One run WITH the suggestion listbox open — that is where
                    # the new listbox a11y lives.
                    tab.evaluate(
                        """(() => {
                          const input = Array.from(document.querySelectorAll('input'))
                            .find(i => (document.querySelector('label[for="' + i.id + '"]')
                              ?.textContent || '').trim().startsWith('Match terms'));
                          if (!input) return false;
                          const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                          setter.call(input, 'cof');
                          input.dispatchEvent(new Event('input', {bubbles: true}));
                          return true;
                        })()""",
                    )
                    tab.sleep(1.4)
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
                tab.screenshot(str(HERE / f"m122-bill-{width}-{theme}.png"))

        # leave the evidence pointing at the final state
        report["final"] = {
            "rogers_bill_id": rogers_id,
            "suggest_bare_top": (bare[:3] if bare else []),
            "stats": detail.get("stats"),
        }
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

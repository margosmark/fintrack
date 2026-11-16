"""M113 — the live probe for BOTH fixes, against the deployed stack (AGENTS §4 step 5).

    python3 m113_pending_import_probe.py [--base https://fintrac.princessandmark.com]
                                         [--file /path/to/real-tfsa.csv]
                                         [--resp-file /path/to/real-resp.csv]

A THROWAWAY tenant (register + login + one TFSA account) imports **Mark's real
TFSA export** byte-for-byte, because the demo tenant answers 403
`demo_account_restricted` on every write and Mark's own password is not ours
to use. What each check proves:

  1 before    the PRE-M113 parser (git 3b17b95, loaded from `git show`) reports
              the exact rows the bug did: 20 unmapped of 91 for the TFSA file
              (4 TRANSFER_TF + 16 Tax/NRT) and 1 of 20 for the RESP file
  2 preview   `POST /trades/import/preview` (dry run, zero writes) answers
              `summary.unmapped == 0` for the same bytes — through the LEGACY
              parser the wizard runs by default (`profile: null`)
  3 stage     `POST /trades/import` stages it: `unmapped == []`, `imported == 91`
  4 banner    `/accounts/<id>` renders the pending-import banner — count, copy,
              `role=status` + `aria-live=polite`, `/imports` link, >=44px target
  5 link      clicking it lands on `/imports` (the review surface)
  6 terminal  the batch leaves `pending_review`: commit when the fx cache lets
              it (see the note below), otherwise the documented 422
              `fx_rate_missing` is RECORDED and the batch is discarded — never
              a 500 — and the banner is GONE either way (the count is 0)
  7 commit    Mark's RESP export (all CAD, so no fx date can block it) imports
              AND commits on the deployed stack: the full ACB replay,
              including the quantity-0 cash transfer_out this card guards
  8 themes    both themes screenshot; console errors 0
  9 untouched the operator's watched account keeps exactly the batches it had
              before the probe ran (operator psql, read-only, enabled with
              --watch-account-id) — the probe never writes to that tenant

**The fx note.** The TFSA file needs a USD/CAD rate on 19 trade dates. The
shared `fx_rates` cache holds 256 USD/CAD rows (2025-09-25 … 2026-09-24) and is
missing exactly one: **2025-12-26**, a date the BoC publishes no rate for. docs/06
pins that as a hard `422 fx_rate_missing` ("add a manual rate") rather than a
guessed carry-forward — a pre-existing rule this card does not touch, so the
probe records the outcome instead of working around it.

Exit 0 only if every check passes; JSON + PNG evidence land next to this script.
"""

from __future__ import annotations

import argparse
import base64
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

# Throwaway tenant credentials (same pattern as m110_carry_probe) — never a
# real account, never reused anywhere else.
PASSWORD = random_password("m113")
DEFAULT_FILE = Path(
    "/home/hermes/.hermes/profiles/pm/cache/documents/doc_84b666942adf_tfsa.csv"
)
DEFAULT_RESP_FILE = Path(
    "/home/hermes/.hermes/profiles/pm/cache/documents/"
    "doc_c4d4510ffb5c_activities-export-2026-10-07.csv"
)


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


def psql(sql: str) -> str:
    remote = (
        "docker exec -i fin_tracker-db-1 psql -U fintrack -d fintrack -tA -c "
        + shlex.quote(sql)
    )
    out = subprocess.run(["ssh", "example.internal", remote], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr[:400]}")
    return out.stdout.strip()


#: Runs the PRE-FIX parser (git 3b17b95) on the same bytes — the "before"
#: half of the A/B. It needs the backend venv (sqlmodel), while this probe
#: needs system python3 (websocket-client), so it runs in a subprocess.
_BEFORE_MEASURE = """
import importlib.util, json, subprocess, sys
from pathlib import Path

repo = Path({repo!r})
csv_path = Path({csv!r})
blob = subprocess.run(
    ["git", "-C", str(repo), "show", "3b17b95:backend/app/importers/investment_profile.py"],
    capture_output=True, text=True, check=True,
).stdout
scratch = Path("/tmp/m113_pre_fix_investment_profile.py")
scratch.write_text(blob, encoding="utf-8")
sys.path.insert(0, str(repo / "backend"))
spec = importlib.util.spec_from_file_location("m113_pre_fix", scratch)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
# Registered BEFORE exec: dataclasses resolves annotations through
# sys.modules[cls.__module__] and raises on a module that is not there.
sys.modules["m113_pre_fix"] = module
spec.loader.exec_module(module)
rows, unmapped = module.parse_investment_csv(csv_path.read_bytes())
kinds = {{}}
for row in unmapped:
    key = f"{{row.activity_type}}/{{row.activity_sub_type}}"
    kinds[key] = kinds.get(key, 0) + 1
print(json.dumps({{"rows": len(rows), "unmapped": len(unmapped), "by_kind": kinds}}))
"""


def pre_m113_unmapped(csv_path: Path) -> dict:
    """Parse the file with the PRE-FIX parser, loaded straight from git."""
    repo = HERE.parents[2]
    script = _BEFORE_MEASURE.format(repo=str(repo), csv=str(csv_path))
    out = subprocess.run(
        ["uv", "run", "--no-sync", "python", "-c", script],
        cwd=str(repo / "backend"),
        capture_output=True,
        text=True,
        timeout=240,
    )
    if out.returncode != 0:
        raise RuntimeError(f"before-measurement failed: {out.stderr[-600:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- #
# Deployed-API helpers — ONE token for the whole probe (login is 10/minute).
# --------------------------------------------------------------------------- #


def seed(tab, stamp: int) -> dict:
    """Register, sign in, create the TFSA account the file imports into."""
    email = f"m113-probe-{stamp}@example.com"
    return tab.evaluate(
        f"""(async () => {{
      const email = {json.dumps(email)}, password = {json.dumps(PASSWORD)};
      const h = {{'Content-Type': 'application/json'}};
      const reg = await fetch('/api/v1/auth/register',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const log = await fetch('/api/v1/auth/login',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const token = (await log.json()).access_token;
      const auth = {{...h, Authorization: 'Bearer ' + token}};
      const acct = await fetch('/api/v1/accounts',
        {{method:'POST', headers:auth,
          body: JSON.stringify({{name:'M113 probe TFSA', type:'tfsa', currency:'CAD'}})}});
      const account = await acct.json();
      return {{email, token, register: reg.status, login: log.status,
               account_id: account.id, account_status: acct.status}};
    }})()""",
        await_promise=True,
    )


def create_account(tab, token: str, name: str, type_: str) -> dict:
    return tab.evaluate(
        f"""(async () => {{
      const auth = {{'Content-Type': 'application/json',
                    Authorization: 'Bearer {token}'}};
      const r = await fetch('/api/v1/accounts',
        {{method:'POST', headers:auth,
          body: JSON.stringify({{name: {json.dumps(name)},
                                 type: {json.dumps(type_)},
                                 currency: 'CAD'}})}});
      const body = await r.json();
      return {{status: r.status, account_id: body.id}};
    }})()""",
        await_promise=True,
    )


def run_import(tab, token: str, account_id: int, csv_path: Path) -> dict:
    """Dry run, then stage the file, through the deployed API."""
    payload = base64.b64encode(csv_path.read_bytes()).decode()
    return tab.evaluate(
        f"""(async () => {{
      const auth = {{Authorization: 'Bearer {token}'}};
      const bytes = Uint8Array.from(atob({json.dumps(payload)}), c => c.charCodeAt(0));
      const fd = () => {{
        const f = new FormData();
        f.append('file', new Blob([bytes], {{type: 'text/csv'}}), 'm113-real-export.csv');
        f.append('account_id', String({account_id}));
        return f;
      }};
      const prev = await fetch('/api/v1/trades/import/preview',
        {{method:'POST', headers:auth, body: fd()}});
      const preview = await prev.json();
      const stage = await fetch('/api/v1/trades/import',
        {{method:'POST', headers:auth, body: fd()}});
      const staged = await stage.json();
      return {{
        preview_status: prev.status,
        preview_unmapped: preview.summary ? preview.summary.unmapped : null,
        preview_rows: preview.summary ? preview.summary.total : null,
        preview_profile: preview.profile,
        stage_status: stage.status,
        stage_unmapped: Array.isArray(staged.unmapped) ? staged.unmapped.length : null,
        stage_unmapped_reasons: (staged.unmapped || []).slice(0, 5).map(u => u.reason),
        imported: staged.imported,
        duplicates_skipped: staged.duplicates_skipped,
        batch_id: staged.batch_id,
        institution: staged.institution,
      }};
    }})()""",
        await_promise=True,
    )


def commit_batch(tab, token: str, batch_id: int) -> dict:
    result = tab.evaluate(
        f"""(async () => {{
      const r = await fetch('/api/v1/trades/import/{batch_id}/commit',
        {{method:'POST',
          headers:{{Authorization: 'Bearer {token}',
                   'Content-Type': 'application/json'}},
          body: JSON.stringify({{}})}});
      return {{status: r.status, body: await r.json()}};
    }})()""",
        await_promise=True,
    )
    assert isinstance(result, dict)
    return result


def discard_batch(tab, token: str, batch_id: int) -> dict:
    result = tab.evaluate(
        f"""(async () => {{
      const r = await fetch('/api/v1/trades/import/{batch_id}/discard',
        {{method:'POST', headers:{{Authorization: 'Bearer {token}'}}}});
      return {{status: r.status, body: await r.json()}};
    }})()""",
        await_promise=True,
    )
    assert isinstance(result, dict)
    return result


# --------------------------------------------------------------------------- #
# The browser half — login, banner, link, themes
# --------------------------------------------------------------------------- #


def ui_login(tab, base: str, email: str) -> None:
    tab.navigate(f"{base}/login")
    settle(tab)
    tab.evaluate('document.querySelector(\'input[type="email"]\').focus()')
    tab.type_text(email)
    tab.evaluate('document.querySelector(\'input[type="password"]\').focus()')
    tab.type_text(PASSWORD)
    tab.press("Enter")
    tab.wait_for("location.pathname !== '/login'", timeout=25)
    tab.sleep(1.5)


def banner_state(tab) -> dict | None:
    return tab.evaluate(
        """(() => {
      const b = document.querySelector('[data-testid="pending-import-banner"]');
      if (!b) return null;
      const link = b.querySelector('a[href="/imports"]');
      return {
        text: (b.innerText || '').trim(),
        role: b.getAttribute('role'),
        live: b.getAttribute('aria-live'),
        href: link ? link.getAttribute('href') : null,
        link_height: link ? Math.round(link.getBoundingClientRect().height) : 0,
      };
    })()"""
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--resp-file", default=str(DEFAULT_RESP_FILE))
    ap.add_argument(
        "--watch-account-id",
        type=int,
        default=None,
        help="operator account for the read-only untouched check (no default id)",
    )
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m113_pending_import_probe.py")
    csv_path = Path(args.file)
    resp_path = Path(args.resp_file)
    assert csv_path.exists(), f"missing file: {csv_path}"
    assert resp_path.exists(), f"missing file: {resp_path}"
    stamp = int(time.time())

    watch_id = args.watch_account_id
    before_account = (
        psql(
            f"SELECT status || '=' || count(*) FROM import_batches "
            f"WHERE account_id={watch_id} GROUP BY status ORDER BY status;"
        )
        if watch_id
        else None
    )
    after_account = None  # evidence key stays present (null) when not watching
    old_tfsa = pre_m113_unmapped(csv_path)
    check(
        "before (pre-M113 parser, TFSA)",
        old_tfsa["unmapped"] == 20,
        f"{old_tfsa['unmapped']} unmapped of {old_tfsa['rows'] + old_tfsa['unmapped']} rows: "
        f"{old_tfsa['by_kind']}",
    )
    old_resp = pre_m113_unmapped(resp_path)
    check(
        "before (pre-M113 parser, RESP)",
        old_resp["unmapped"] == 1,
        f"{old_resp['unmapped']} unmapped of {old_resp['rows'] + old_resp['unmapped']} rows: "
        f"{old_resp['by_kind']}",
    )

    browser = Browser(width=args.width, height=900)
    try:
        tab = browser.new_tab(args.base)
        tab.navigate(args.base)
        seed_result = seed(tab, stamp)
        check(
            "seed",
            seed_result["account_status"] == 201 and seed_result["login"] == 200,
            json.dumps({k: v for k, v in seed_result.items() if k != "token"}),
        )
        email = seed_result["email"]
        token = seed_result["token"]
        account_id = seed_result["account_id"]

        imported = run_import(tab, token, account_id, csv_path)
        check(
            "preview 0 unmapped (was 20)",
            imported["preview_status"] == 201 and imported["preview_unmapped"] == 0,
            f"status={imported['preview_status']} unmapped={imported['preview_unmapped']} "
            f"rows={imported['preview_rows']} profile={imported['preview_profile']}",
        )
        check(
            "stage 0 unmapped / 91 imported",
            imported["stage_status"] == 201
            and imported["stage_unmapped"] == 0
            and imported["imported"] == 91,
            f"status={imported['stage_status']} unmapped={imported['stage_unmapped']} "
            f"imported={imported['imported']} batch={imported['batch_id']} "
            f"reasons={imported['stage_unmapped_reasons']}",
        )
        batch_id = imported["batch_id"]

        # --- the banner on the account page, light theme -----------------
        ui_login(tab, args.base, email)
        tab.evaluate("try { localStorage.setItem('theme','light') } catch (e) {}")
        tab.navigate(f"{args.base}/accounts/{account_id}")
        settle(tab)
        light = banner_state(tab)
        check(
            "banner renders while pending",
            light is not None and light["text"].startswith("1 uncommitted import for TFSA"),
            json.dumps(light) if light else "banner missing",
        )
        check(
            "banner a11y + link contract",
            light is not None
            and light["role"] == "status"
            and light["live"] == "polite"
            and light["href"] == "/imports"
            and light["link_height"] >= 44,
            json.dumps(light) if light else "banner missing",
        )
        light_shot = str(HERE / "m113-banner-prod-1440-light.png")
        tab.screenshot(light_shot)

        tab.evaluate("try { localStorage.setItem('theme','dark') } catch (e) {}")
        tab.navigate(f"{args.base}/accounts/{account_id}")
        settle(tab)
        dark = banner_state(tab)
        check("banner in dark theme", dark is not None, json.dumps(dark) if dark else "missing")
        dark_shot = str(HERE / "m113-banner-prod-1440-dark.png")
        tab.screenshot(dark_shot)

        # --- the link routes to /imports ---------------------------------
        tab.evaluate(
            """(() => {
              const l = document.querySelector('[data-testid="pending-import-banner"] a[href="/imports"]');
              if (l) l.scrollIntoView({block: 'center'});
              return !!l;
            })()"""
        )
        tab.sleep(0.4)
        rect = tab.evaluate(
            """(() => {
              const l = document.querySelector('[data-testid="pending-import-banner"] a[href="/imports"]');
              if (!l) return null;
              const r = l.getBoundingClientRect();
              return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            })()"""
        )
        if rect:
            assert isinstance(rect, dict)
            tab.click_at(rect["x"], rect["y"])
            tab.wait_for("location.pathname === '/imports'", timeout=20)
            tab.sleep(1.2)
            check(
                "link routes to /imports",
                tab.evaluate("location.pathname") == "/imports",
                str(tab.evaluate("location.pathname")),
            )
        else:
            check("link routes to /imports", False, "link not found")

        # --- the batch leaves pending_review, and the banner goes ---------
        commit = commit_batch(tab, token, batch_id)
        check(
            "commit answers 200 or the documented 422 (never a 500)",
            commit["status"] in (200, 422),
            f"status={commit['status']} body={json.dumps(commit['body'])[:240]}",
        )
        discarded = None
        if commit["status"] != 200:
            discarded = discard_batch(tab, token, batch_id)
            check(
                "discard clears the batch",
                discarded["status"] == 200,
                f"status={discarded['status']} body={json.dumps(discarded['body'])[:160]}",
            )
        tab.navigate(f"{args.base}/accounts/{account_id}")
        settle(tab)
        after = banner_state(tab)
        check(
            "banner gone once the batch is no longer pending",
            after is None,
            json.dumps(after) if after else "absent",
        )

        # --- Mark's RESP export: import AND commit (all CAD) --------------
        resp_account = create_account(tab, token, "M113 probe RESP", "brokerage_non_registered")
        check("resp account", resp_account["status"] == 201, json.dumps(resp_account))
        resp_imported = run_import(tab, token, resp_account["account_id"], resp_path)
        # The real file is 20 rows: 18 staged + 2 exact duplicates (its two
        # identical 2026-01-05 deposits dedupe inside the file, docs/03).
        check(
            "RESP preview/stage 0 unmapped (was 1)",
            resp_imported["preview_status"] == 201
            and resp_imported["preview_unmapped"] == 0
            and resp_imported["stage_unmapped"] == 0
            and resp_imported["imported"] + resp_imported["duplicates_skipped"] == 20,
            f"preview_unmapped={resp_imported['preview_unmapped']} "
            f"stage_unmapped={resp_imported['stage_unmapped']} "
            f"imported={resp_imported['imported']} "
            f"duplicates={resp_imported['duplicates_skipped']} "
            f"reasons={resp_imported['stage_unmapped_reasons']}",
        )
        resp_commit = commit_batch(tab, token, resp_imported["batch_id"])
        check(
            "RESP commit runs the ACB replay (incl. qty-0 cash transfer_out)",
            resp_commit["status"] == 200,
            f"status={resp_commit['status']} body={json.dumps(resp_commit['body'])[:240]}",
        )

        errors = tab.console_errors()
        check("no console errors", len(errors) == 0, "; ".join(errors[:3]))

        if watch_id:
            after_account = psql(
                f"SELECT status || '=' || count(*) FROM import_batches "
                f"WHERE account_id={watch_id} GROUP BY status ORDER BY status;"
            )
            check(
                "watched account untouched by the probe",
                after_account == before_account,
                f"before={before_account!r} after={after_account!r}",
            )
        else:
            CHECKS.append(
                {
                    "check": "watched account untouched by the probe",
                    "ok": True,
                    "detail": "SKIPPED — pass --watch-account-id (read-only psql)",
                }
            )
            print(
                "SKIP  watched account untouched by the probe — no --watch-account-id",
                flush=True,
            )

        evidence = {
            "milestone": "M113",
            "base": args.base,
            "width": args.width,
            "file": str(csv_path),
            "resp_file": str(resp_path),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "before_parser": {"tfsa": old_tfsa, "resp": old_resp},
            "tfsa_import": imported,
            "banner_light": light,
            "banner_dark": dark,
            "tfsa_commit": {"status": commit["status"], "body": commit["body"]},
            "tfsa_discard": discarded,
            "resp_import": resp_imported,
            "resp_commit": resp_commit,
            "watched_account_batches_before": before_account,
            "watched_account_batches_after": after_account,
            "screenshots": [light_shot, dark_shot],
            "checks": CHECKS,
        }
        out = HERE / "m113-pending-import-probe.json"
        out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(f"evidence: {out}")
    finally:
        browser.close()

    failed = [c for c in CHECKS if not c["ok"]]
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

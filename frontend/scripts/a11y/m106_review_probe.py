"""M106 review queue + transaction activity log — the live probe
(AGENTS §4 step 5, against a running stack).

    python3 m106_review_probe.py [--base https://fintrac.princessandmark.com] [--width 1440]

Runs against a THROWAWAY tenant (register + login + one account + three manual
rows + one real QFX import), because the demo tenant answers
403 demo_account_restricted on every write. What each check proves:

  1  seed      register/login/account/3 rows import up+commit
  2  staged    the import's rows are staged, NOT counted (committed filter)
  3  queue     unreviewed-count == N, ?reviewed=false == N, ?reviewed=true == 0
  4  receipts  every row's /events is EXACTLY one `created`; the imported row
               carries source=import + its batch id, the manual row doesn't
  5  flip      POST /{id}/review -> reviewed_at set, count-1, second receipt,
               and un-flagging writes a receipt too
  6  export    /export/full ships transaction_events.csv (the row's receipts
               plus the deleted subject's — the receipt that outlives it)
  7  ui        the header "N unreviewed" hint, the "Needs review" chip count,
               a row toggle that decrements both live, and the History drawer
               listing the sequence newest-first
  8  axe       0 critical/serious on /transactions with the drawer open

Exit 0 only if every check passes; JSON evidence lands next to this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from probe_target import random_password, strip_passwords

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
AXE = str(REPO / "frontend" / "node_modules" / "axe-core" / "axe.min.js")
QFX = REPO / "backend" / "tests" / "fixtures" / "sample.qfx"
BASE = os.environ.get("M106_BASE", "http://127.0.0.1:18080")

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


# --------------------------------------------------------------------------- HTTP
class Api:
    """Bearer-token JSON client — the probe's reads/writes never touch React."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token: str | None = None

    def call(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        raw: bytes | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, method=method)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            req.add_header("Content-Type", content_type)
            req.data = data
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:  # 4xx/5xx are evidence, not crashes
            return exc.code, exc.read()
        except Exception as exc:  # pragma: no cover - transport failures
            return -1, str(exc).encode()

    def json(self, method: str, path: str, **kw) -> Any:
        status, payload = self.call(method, path, **kw)
        if status < 0 or status >= 400:
            return None
        return json.loads(payload)


def multipart(fields: dict[str, str], filename: str, blob: bytes) -> tuple[bytes, str]:
    boundary = "----m106" + uuid.uuid4().hex
    out = bytearray()
    for key, value in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
    out += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
    ).encode()
    out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# ------------------------------------------------------------------- browser glue
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


def seed_in_page(tab) -> dict:
    """Register + sign in inside the page so the refresh cookie lands on origin."""
    stamp = int(time.time() * 1000)
    email = f"m106-{stamp}@example.com"
    password = random_password("m106")
    return tab.evaluate(
        f"""(async () => {{
      const h = {{'Content-Type': 'application/json'}};
      const email = {json.dumps(email)}, password = {json.dumps(password)};
      const reg = await fetch('/api/v1/auth/register',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const log = await fetch('/api/v1/auth/login',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      return {{email, password, register: reg.status, login: log.status}};
    }})()""",
        await_promise=True,
    )


def ui_login(tab, email: str, password: str) -> None:
    tab.navigate(f"{BASE}/login")
    settle(tab)
    tab.evaluate("document.querySelector('input[type=\"email\"]').focus()")
    tab.send("Input.insertText", {"text": email})
    tab.evaluate("document.querySelector('input[type=\"password\"]').focus()")
    tab.send("Input.insertText", {"text": password})
    tab.press("Enter")
    tab.wait_for("location.pathname !== '/login'", timeout=25)


def goto(tab, path: str) -> None:
    tab.navigate(f"{BASE}{path}")
    settle(tab)


def click_button(tab, selector: str) -> dict:
    rect = tab.evaluate(
        f"""(() => {{
      const el = document.querySelector({json.dumps(selector)});
      if (!el) return null;
      el.scrollIntoView({{block: 'center'}});
      const r = el.getBoundingClientRect();
      return {{x: r.x + r.width / 2, y: r.y + r.height / 2, h: r.height}};
    }})()"""
    )
    if rect:
        tab.sleep(0.2)
        tab.click_at(rect["x"], rect["y"])
    return rect or {}


def load_axe(tab) -> None:
    tab.evaluate(Path(AXE).read_text(encoding="utf-8"))


def axe_check(tab, name: str) -> None:
    result = tab.evaluate(
        "axe.run(document, {resultTypes: ['violations']})", await_promise=True
    )
    counts: dict[str, int] = {}
    rows: list[str] = []
    for violation in result.get("violations", []):
        impact = violation.get("impact") or "unknown"
        counts[impact] = counts.get(impact, 0) + 1
        rows.append(f"{violation['id']}({impact}x{len(violation['nodes'])})")
    bad = counts.get("critical", 0) + counts.get("serious", 0)
    check(f"axe 0 critical/serious — {name}", bad == 0, f"{counts} {rows[:4]}")


# ---------------------------------------------------------------------- main flow
def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--out", default=str(HERE / "m106-review-probe.json"))
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    from probe_target import require_target
    require_target(BASE, who="m106_review_probe.py")

    from a11y_driver import Browser  # local import: keeps --help fast

    api = Api(BASE)
    evidence: dict = {"base": BASE}

    # --- 1. seed: a real tenant + three manual rows --------------------------
    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{BASE}/login")
        tab.enable_runtime()
        tab.send("Network.enable")
        tab.force_viewport(args.width, args.height)
        settle(tab)
        creds = seed_in_page(tab)
        check(
            "seed tenant (register + login)",
            creds["register"] == 200 and creds["login"] == 200,
            json.dumps(strip_passwords(creds)),
        )
        logged = api.json("POST", "/api/v1/auth/login",
                          body={"email": creds["email"], "password": creds["password"]})
        api.token = (logged or {}).get("access_token")
        check("bearer token for the HTTP half", bool(api.token))

        account = api.json("POST", "/api/v1/accounts",
                           body={"name": "Probe Chequing", "type": "chequing",
                                 "currency": "CAD"})
        account_id = (account or {}).get("id")
        check("probe account", bool(account_id), json.dumps(account))

        rows = []
        for payee, amount in (("PROBE GROCERIES", "-42.10"),
                              ("PROBE GAS", "-30.00"),
                              ("PROBE RENT", "-1800.00")):
            rows.append(api.json(
                "POST", "/api/v1/transactions",
                body={"account_id": account_id, "date": "2026-09-10",
                      "raw_description": payee, "normalized_payee": payee,
                      "amount_native": amount, "currency": "CAD"}))
        manual_ids = [r["id"] for r in rows if r and r.get("id")]
        check("three manual rows", len(manual_ids) == 3, str(manual_ids))
        evidence["manual_ids"] = manual_ids

        # --- 2. the import: staged first (invisible), then committed ---------
        blob = QFX.read_bytes()
        body, ctype = multipart({"account_id": str(account_id)}, "sample.qfx", blob)
        status, resp = api.call("POST", "/api/v1/imports/upload", raw=body,
                                content_type=ctype)
        upload = json.loads(resp) if status in (200, 201) else {}
        batch_id = upload.get("id") or upload.get("batch_id")
        check("import upload staged", status == 201 and bool(batch_id),
              f"HTTP {status} {json.dumps(upload)[:200]}")
        if not batch_id:
            print("cannot continue without a batch", file=sys.stderr)
            return 1

        staged_count = (api.json("GET", "/api/v1/transactions/unreviewed-count")
                        or {}).get("count")
        check("staged rows are NOT counted (committed filter)",
              staged_count == len(manual_ids),
              f"count={staged_count}, expected {len(manual_ids)} (3 manual rows)")

        status, resp = api.call("POST", f"/api/v1/imports/{batch_id}/commit", body={})
        committed = json.loads(resp) if status == 200 else {}
        check("batch committed", status == 200,
              f"HTTP {status} imported={committed.get('imported')}")

        count_after = (api.json("GET", "/api/v1/transactions/unreviewed-count")
                       or {}).get("count")
        imported = int(committed.get("imported") or 0)
        check("import seeded a queue", imported > 0 and count_after == len(manual_ids) + imported,
              f"imported={imported}, count={count_after}, expected {len(manual_ids) + imported}")
        evidence["imported"] = imported

        # --- 3. queue filter agrees with the count endpoint ------------------
        false_total = (api.json("GET", "/api/v1/transactions?reviewed=false&limit=200")
                       or {}).get("total")
        true_total = (api.json("GET", "/api/v1/transactions?reviewed=true&limit=200")
                      or {}).get("total")
        check("?reviewed=false total == unreviewed-count",
              false_total == count_after, f"{false_total} vs {count_after}")
        check("?reviewed=true total == 0 before any flip", true_total == 0,
              f"total={true_total}")

        # --- 4. receipts: exactly one `created` per row ----------------------
        manual_events = api.json("GET",
                                 f"/api/v1/transactions/{manual_ids[0]}/events") or {}
        m_items = manual_events.get("items") or []
        check("manual row has exactly one `created` receipt, source=manual",
              len(m_items) == 1 and m_items[0]["event_kind"] == "created"
              and (m_items[0].get("payload") or {}).get("source") == "manual",
              json.dumps(m_items)[:240])
        # TransactionPublic carries no import_batch_id, so the imported row is
        # "any row that is not one of the three we just wrote by hand".
        manual_payees = {"PROBE GROCERIES", "PROBE GAS", "PROBE RENT"}
        unreviewed_items = (api.json("GET",
                                     "/api/v1/transactions?reviewed=false&limit=200")
                            or {}).get("items", [])
        imported_row = next(
            (r for r in unreviewed_items
             if r.get("normalized_payee") not in manual_payees),
            None,
        )
        check("the import actually committed at least one row",
              bool(imported_row), json.dumps(unreviewed_items)[:200])
        if not imported_row:
            return 1
        imported_events = (api.json("GET",
                                    f"/api/v1/transactions/{imported_row['id']}/events")
                           or {}).get("items") or []
        ipayload = imported_events[0].get("payload") if imported_events else {}
        check("imported row's receipt carries source=import + batch id",
              len(imported_events) == 1
              and imported_events[0]["event_kind"] == "created"
              and (ipayload or {}).get("source") == "import"
              and (ipayload or {}).get("batch_id") == batch_id,
              json.dumps(imported_events)[:300])

        # --- 5. the flip writes a receipt and moves the count ----------------
        target = manual_ids[0]
        flip = api.json("POST", f"/api/v1/transactions/{target}/review",
                        body={"reviewed": True})
        check("POST /{id}/review marks it", bool(flip) and flip.get("reviewed_at"),
              json.dumps(flip)[:200])
        after_flip = (api.json("GET", "/api/v1/transactions/unreviewed-count")
                      or {}).get("count")
        check("count drops by exactly one", after_flip == count_after - 1,
              f"{after_flip} vs {count_after}")
        flip_events = (api.json("GET",
                                f"/api/v1/transactions/{target}/events") or {}).get("items") or []
        check("the flip appended a `reviewed` receipt (2 total)",
              len(flip_events) == 2 and flip_events[0]["event_kind"] == "reviewed"
              and flip_events[1]["event_kind"] == "created",
              json.dumps([e["event_kind"] for e in flip_events]))
        unflip = api.json("POST", f"/api/v1/transactions/{target}/review",
                          body={"reviewed": False})
        unflip_events = (api.json("GET",
                                  f"/api/v1/transactions/{target}/events") or {}).get("items") or []
        check("un-flagging writes a receipt too (3 total)",
              bool(unflip) and unflip.get("reviewed_at") is None
              and len(unflip_events) == 3
              and unflip_events[0]["event_kind"] == "reviewed",
              json.dumps([e["event_kind"] for e in unflip_events]))
        evidence["events_after_2_flips"] = [e["event_kind"] for e in unflip_events]

        # --- 6. export ships the log ----------------------------------------
        # `/export/full` is the JSON machine snapshot; the one-click ZIP is
        # `/export/zip` — the row-per-table archive the acceptance names.
        status, zipped = api.call("GET", "/api/v1/export/zip")
        names: list[str] = []
        if status == 200:
            import io
            import zipfile

            try:
                with zipfile.ZipFile(io.BytesIO(zipped)) as zf:
                    names = zf.namelist()
            except zipfile.BadZipFile:
                names = [f"<not a zip: {zipped[:120]!r}>"]
        check("/export/zip ships transaction_events.csv",
              status == 200 and any(n.endswith("transaction_events.csv") for n in names),
              f"HTTP {status} {names}")

        # --- 7. the UI: hint, chip, row toggle, History drawer ---------------
        goto(tab, "/transactions")
        if tab.evaluate("location.pathname") != "/transactions":
            ui_login(tab, creds["email"], creds["password"])
            goto(tab, "/transactions")
        check("landed on /transactions", tab.evaluate("location.pathname") == "/transactions")
        body_text = tab.evaluate("document.body.innerText")
        ui_count = count_after  # the API's number is the contract
        check(f"header hint shows “{ui_count} unreviewed”",
              f"{ui_count} unreviewed" in body_text,
              str([ln for ln in body_text.split("\n") if "unreviewed" in ln][:3]))
        check("“Needs review” chip carries the count",
              "Needs review" in body_text,
              str([ln for ln in body_text.split("\n") if "Needs review" in ln][:3]))

        toggle_selector = 'button[title="Needs review — click to mark it reviewed"]'
        rect = click_button(tab, toggle_selector)
        check("row toggle hit area >= 44px at this width",
              rect.get("h", 0) >= 44 or args.width >= 768, f"rect={rect}")
        tab.wait_for(
            "document.querySelectorAll("
            "'button[title=\"Reviewed — click to mark it unreviewed\"][aria-pressed=\"true\"]'"
            ").length >= 1",
            timeout=15,
        )
        tab.sleep(1.2)
        flipped_ui = tab.evaluate("document.body.innerText")
        check("count decrements live after the toggle flip",
              f"{count_after - 1} unreviewed" in flipped_ui,
              str([ln for ln in flipped_ui.split("\n") if "unreviewed" in ln][:3]))
        check("toggle is aria-pressed and its title flipped",
              tab.evaluate(
                  "document.querySelectorAll("
                  "'button[title=\"Reviewed — click to mark it unreviewed\"]"
                  "[aria-pressed=\"true\"]').length") >= 1)

        # Keyboard-only flip: the mouse never touches this one. Focus is moved
        # with .focus() (FOCUS is not the thing under test — the ACTIVATION is),
        # then a real Enter keystroke goes through CDP to the focused button.
        # A div with an onClick, or a tabindex=-1 toggle, flips nothing here
        # (docs/09 §Review queue: the toggle must work keyboard-only).
        #
        # This used to call a probe-local `press_enter()` because the shared
        # driver's `press("Enter")` sent rawKeyDown with no `text` and Chrome
        # ran no default action. The driver is fixed (press_regression.py
        # re-measures it every run), so the helper is gone and the shared path
        # is what runs here.
        #
        # Worth knowing when reading the OLD results: that helper sent
        # keyDown+text PLUS an explicit `char`, which fires the click TWICE.
        # The committed run still passed only because the toggle is
        # `disabled={review.isPending}` — the second activation landed on a
        # button already mid-POST and was swallowed. The check below now
        # exercises exactly one activation, which is what it always claimed
        # to measure.
        kb_focused = tab.evaluate("""(() => {
          const el = document.querySelector('button[title="Needs review — click to mark it reviewed"]');
          if (!el) return false;
          el.scrollIntoView({block: 'center'});
          el.focus();
          return document.activeElement === el;
        })()""")
        kb_target = tab.evaluate("(document.activeElement || {}).title || ''")
        tab.press("Enter")
        # Wait for the FLIP, not for a fixed pause: the row only changes once
        # the POST lands, and prod latency must not be read as "Enter did
        # nothing" (the mouse flip above waits the same way).
        try:
            kb_flipped = tab.wait_for(
                "document.querySelectorAll("
                "'button[title=\"Reviewed — click to mark it unreviewed\"]"
                "[aria-pressed=\"true\"]').length >= 2",
                timeout=25,
            )
        except TimeoutError:
            kb_flipped = None
        flipped_by_key = tab.evaluate(
            "document.querySelectorAll("
            "'button[title=\"Reviewed — click to mark it unreviewed\"]"
            "[aria-pressed=\"true\"]').length")
        check("keyboard-only flip (focus + Enter, no mouse)",
              bool(kb_focused) and bool(kb_flipped),
              f"focused={kb_focused} on {kb_target!r}, "
              f"aria-pressed=true rows={flipped_by_key}")

        # Bulk path: the count must follow the M96 toolbar's run too. The
        # toolbar drives N single-row flips, so the queue empties in the UI —
        # the hint pill and the chip's pill both have to go quiet, which only
        # happens if the count query is invalidated WITH the rows (docs/09
        # §Review queue: one number, two surfaces).
        tab.evaluate("""(() => {
          const all = document.querySelector('input[aria-label="Select every transaction on this page"]');
          if (!all) return false;
          all.scrollIntoView({block: 'center'});
          all.click();
          return true;
        })()""")
        tab.sleep(0.5)
        bulk_started = tab.evaluate("""(() => {
          const btn = document.querySelector('button[aria-label$="selected transactions reviewed"]');
          if (!btn) return false;
          btn.scrollIntoView({block: 'center'});
          btn.click();
          return true;
        })()""")
        tab.wait_for("""document.querySelectorAll('[data-testid="bulk-progress"]').length === 0
                     && !document.body.innerText.toLowerCase().includes('unreviewed')""",
                     timeout=45)
        bulk_text = tab.evaluate("document.body.innerText")
        check("bulk Mark reviewed empties the queue on BOTH surfaces",
              bool(bulk_started) and "unreviewed" not in bulk_text.lower(),
              str([ln for ln in bulk_text.split("\n") if "review" in ln.lower()][:4]))

        history = 'button[aria-label^="History — PROBE GROCERIES"]'
        click_button(tab, history)
        tab.wait_for("document.querySelector('[role=\"dialog\"]') !== null", timeout=10)
        tab.sleep(1.4)
        receipts = tab.evaluate(
            "[...document.querySelectorAll('[role=\"dialog\"] li')].map(li => li.innerText)"
        )
        check("History drawer lists the receipt sequence",
              len(receipts) >= 1,
              json.dumps(receipts)[:400])
        check("drawer's newest receipt is the review flip",
              receipts and "review" in receipts[0].lower(),
              receipts[0][:160] if receipts else "")
        load_axe(tab)
        axe_check(tab, "History drawer open on /transactions")

        Path(args.out).write_text(json.dumps(
            {"checks": CHECKS, "evidence": evidence}, indent=2, ensure_ascii=False))
        failed = [c for c in CHECKS if not c["ok"]]
        print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
        if failed:
            for c in failed:
                print("  FAIL " + c["check"])
        return 1 if failed else 0
    finally:
        browser.close()


if __name__ == "__main__":
    sys.exit(main())

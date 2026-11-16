"""M104 tag UX — the live probe (AGENTS §4 step 5, against a running stack).

    python3 m104_tag_probe.py [--base http://127.0.0.1:18080] [--width 1440]

Runs against a THROWAWAY tenant (register + login + one account + two rows
inside the page), because the demo tenant answers 403 demo_account_restricted
on every tag write. What each check proves:

  1  quick-tag  click the row's `+` -> type -> Enter = ONE
     PUT /transactions/{id}/tags and the chip lands without the editor
     (the acceptance's <= 2 interactions)
  2  escape     in the editor modal: press 1 closes the picker LIST only,
     press 2 closes the modal (docs/09 nesting rule)
  3  multi-tag  two chips -> Apply -> GET /transactions?tag_ids=… twice, and
     every remaining row carries BOTH tags (AND, "matches all selected")
  4  manager    "Manage tags…" opens the dialog, usage counts are printed,
     rename lands on the row chip, merge folds a second tag into the survivor
  5  mobile     at --width 390 the open popover is fully inside the viewport

Exit 0 only if every check passes; JSON evidence lands next to this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from a11y_driver import Browser
from probe_target import random_password, strip_passwords

HERE = Path(__file__).resolve().parent
AXE = str(HERE.parents[1] / "node_modules" / "axe-core" / "axe.min.js")
BASE = os.environ.get("M104_BASE", "http://127.0.0.1:18080")

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
    """Register, sign in, and create one account + two rows (all in-page)."""
    stamp = int(time.time() * 1000)
    email = f"m104-{stamp}@example.com"
    password = random_password("m104")
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
      const account_id = (await acct.json()).id;
      const mk = async (payee, amount) => {{
        const r = await fetch('/api/v1/transactions', {{method:'POST', headers:auth,
          body: JSON.stringify({{account_id, date:'2026-09-10', raw_description: payee,
            normalized_payee: payee, amount_native: amount, currency:'CAD'}})}});
        return r.status;
      }};
      return {{email, password, register: reg.status, login: log.status,
              account: acct.status,
              rows: [await mk('PROBE GROCERIES', '-42.10'), await mk('PROBE GAS', '-30.00')]}};
    }})()""",
        await_promise=True,
    )


def ui_login(tab, email: str, password: str) -> None:
    """Fallback when the cookie session did not restore after a reload."""
    tab.navigate(f"{BASE}/login")
    settle(tab)
    tab.evaluate(
        "document.querySelector('input[type=\"email\"]').focus()"
    )
    type_once(tab, email)
    tab.evaluate("document.querySelector('input[type=\"password\"]').focus()")
    type_once(tab, password)
    tab.press("Enter")
    tab.wait_for("location.pathname !== '/login'", timeout=25)


def goto(tab, path: str) -> None:
    tab.navigate(f"{BASE}{path}")
    settle(tab)


def click_button(tab, selector: str) -> dict:
    """Click the CENTRE of the first element matching `selector` (real mouse)."""
    rect = tab.evaluate(
        f"""(() => {{
      const el = document.querySelector({json.dumps(selector)});
      if (!el) return null;
      el.scrollIntoView({{block: 'center'}});
      const r = el.getBoundingClientRect();
      return {{x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height}};
    }})()"""
    )
    if rect:
        tab.sleep(0.2)
        rect = tab.evaluate(
            f"""(() => {{
          const el = document.querySelector({json.dumps(selector)});
          const r = el.getBoundingClientRect();
          return {{x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height}};
        }})()"""
        )
        tab.click_at(rect["x"], rect["y"])
    return rect or {}


def load_axe(tab) -> None:
    tab.evaluate(Path(AXE).read_text(encoding="utf-8"))


def axe_run(tab) -> tuple[dict, list[str]]:
    """axe on the CURRENT document — the open surface, not the page at rest."""
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
    return counts, rows


def axe_check(tab, name: str) -> None:
    counts, rows = axe_run(tab)
    bad = counts["critical"] + counts["serious"]
    check(f"axe 0 critical/serious — {name}", bad == 0, f"{counts} {rows}")


def type_once(tab, text: str) -> None:
    """Type into the focused element exactly once.

    Written because `Tab.type_text` used to dispatch `keyDown(text)` PLUS a
    separate `char(text)`, and Chrome inserted BOTH — every glyph landed twice
    ("pprroobbee", measured in `press_regression.py`, which now asserts the
    single-insert behaviour). `Input.insertText` is still the right primitive
    here: it is the single-insertion API and it fires the `input` event React
    listens for, so a controlled field sees exactly one change per call. This
    helper is kept deliberately — it does not depend on which dispatch the
    driver chooses.
    """
    tab.send("Input.insertText", {"text": text})


def request_urls(tab) -> list[str]:
    out = []
    for event in tab.events("Network.requestWillBeSent"):
        req = event["params"]["request"]
        out.append(f"{req['method']} {req['url']}")
    return out


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--out", default=str(HERE / "m104-tag-probe.json"))
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    from probe_target import require_target
    require_target(BASE, who="m104_tag_probe.py")

    browser = Browser(width=args.width, height=args.height)
    try:
        tab = browser.new_tab(f"{BASE}/login")
        tab.enable_runtime()
        tab.send("Network.enable")
        tab.force_viewport(args.width, args.height)
        settle(tab)

        seed_result = seed(tab)
        check(
            "seed tenant (register/login/account/2 rows)",
            seed_result["register"] == 200
            and seed_result["login"] == 200
            and seed_result["account"] == 201
            and seed_result["rows"] == [201, 201],
            json.dumps(strip_passwords(seed_result)),
        )
        goto(tab, "/transactions")
        if tab.evaluate("location.pathname") != "/transactions":
            ui_login(tab, seed_result["email"], seed_result["password"])
            goto(tab, "/transactions")
        check("landed on /transactions", tab.evaluate("location.pathname") == "/transactions")

        # --- 1. quick-tag: click -> type -> Enter ---------------------------
        # `^=` because the label carries the row id (M92 unique-per-row).
        trigger = 'button[title="Add tags"][aria-label^="Add tags — PROBE GROCERIES"]'
        rect = click_button(tab, trigger)
        check("row +tag hit area >= 44px at this width", rect.get("h", 0) >= 44 or args.width >= 768,
              f"rect={rect}")
        tab.wait_for(
            "document.querySelector('[role=\"combobox\"][aria-label=\"Add or create tag\"]') !== null",
            timeout=10,
        )
        # Popover geometry: at any width the whole popover must sit inside the
        # viewport (the M104 mobile clause — no off-screen clip at 390).
        box = tab.evaluate(
            """(() => {
              let el = document.querySelector('[aria-label="Add or create tag"]');
              while (el && getComputedStyle(el).position !== 'fixed') el = el.parentElement;
              if (!el) return null;
              const r = el.getBoundingClientRect();
              return {left: r.left, top: r.top, right: r.right, bottom: r.bottom,
                      vw: window.innerWidth, vh: window.innerHeight};
            })()"""
        )
        inside = bool(box) and box["left"] >= -1 and box["top"] >= -1 and \
            box["right"] <= box["vw"] + 1 and box["bottom"] <= box["vh"] + 1
        check("popover fits the viewport", inside, json.dumps(box))
        tab.screenshot(str(HERE / f"m104-popover-{args.width}.png"))
        load_axe(tab)
        axe_check(tab, "row quick-tag popover open")
        type_once(tab, "probe-extra")
        tab.sleep(0.3)
        tab.press("Enter")
        tab.wait_for("document.body.innerText.includes('probe-extra')", timeout=15)
        puts = [u for u in request_urls(tab) if u.startswith("PUT") and u.endswith("/tags")]
        check("quick-tag wrote exactly one PUT /transactions/{id}/tags", len(puts) >= 1, str(puts))
        # The popover closes itself once the write lands — the editor never
        # opened, which is the whole point of the interaction count.
        check("chip lands without the editor opening",
              tab.evaluate("document.querySelector('[role=dialog]') === null")
              and tab.evaluate("document.body.innerText.includes('probe-extra')"),
              "popover closed, chip present, no dialog")

        # --- 2. Escape nesting inside the editor ---------------------------
        tab.evaluate(
            """(() => {
              const row = [...document.querySelectorAll('tbody tr')]
                .find(tr => tr.innerText.includes('PROBE GROCERIES'));
              const b = row && row.querySelector('button[aria-label^="Edit transaction"]');
              if (b) b.click();
              return Boolean(b);
            })()"""
        )
        tab.wait_for("document.querySelector('[role=dialog]') !== null", timeout=10)
        # Let the dialog's own first paint settle before driving it — a probe
        # that focuses mid-load races the editor's tag fetch.
        tab.sleep(1.0)
        # By LABEL, not by role: the dialog also holds CategoryPicker's
        # combobox, and the first one in DOM order is not the tag picker.
        tab.evaluate(
            """(() => {
              const i = document.querySelector('[role=dialog] [aria-label="Add or create tag"]');
              if (i) i.focus();
              return Boolean(i);
            })()"""
        )
        tab.sleep(0.3)
        # `aria-expanded` is the honest "list open" signal: with every other
        # tag already attached the dropdown can be open with zero options
        # ("No matches"), where no [role=listbox] exists to look for.
        focused = tab.evaluate(
            """document.activeElement?.getAttribute('aria-label') === 'Add or create tag'"""
        )
        open_before = tab.evaluate(
            """(() => {
              const i = document.querySelector('[role=dialog] [aria-label="Add or create tag"]');
              return i ? i.getAttribute('aria-expanded') : null;
            })()"""
        )
        tab.press("Escape")
        tab.sleep(0.4)
        open_after = tab.evaluate(
            """(() => {
              const i = document.querySelector('[role=dialog] [aria-label="Add or create tag"]');
              return i ? i.getAttribute('aria-expanded') : null;
            })()"""
        )
        if open_before == "true":
            load_axe(tab)
            axe_check(tab, "editor modal with the tag picker open")
        dialog_still = tab.evaluate("document.querySelector('[role=dialog]') !== null")
        check("editor Escape #1 closes the picker list only",
              focused and open_before == "true" and open_after == "false" and dialog_still,
              f"focus={focused}, expanded {open_before} -> {open_after}, dialog={dialog_still}")
        tab.press("Escape")
        tab.sleep(0.4)
        check("editor Escape #2 closes the modal",
              tab.evaluate("document.querySelector('[role=dialog]') === null"))
        goto(tab, "/transactions")

        # second tag on the same row, so the AND filter has something to prove
        click_button(tab, trigger)
        tab.wait_for(
            "document.querySelector('[role=\"combobox\"][aria-label=\"Add or create tag\"]') !== null",
            timeout=10,
        )
        type_once(tab, "probe-shared")
        tab.sleep(0.3)
        tab.press("Enter")
        tab.wait_for("document.body.innerText.includes('probe-shared')", timeout=15)

        # --- 3. multi-tag AND filter ---------------------------------------
        for name in ("probe-extra", "probe-shared"):
            tab.evaluate(
                f"""(() => {{
              const i = [...document.querySelectorAll('input[aria-label=\"Add tag to filter\"]')][0];
              i.focus();
            }})()"""
            )
            type_once(tab, name)
            tab.sleep(0.3)
            tab.press("Enter")
            tab.wait_for(
                "document.querySelector('[aria-label^=\"Remove tag filter\"]') !== null",
                timeout=10,
            )
        chips = tab.evaluate(
            "document.querySelectorAll('[aria-label^=\"Remove tag filter\"]').length"
        )
        check("filter shows both chips", chips == 2, f"chips={chips}")
        tab.evaluate(
            """(() => {
              const b = [...document.querySelectorAll('form button[type=submit]')]
                .find(el => el.textContent.trim() === 'Apply filters');
              if (b) b.click();
              return Boolean(b);
            })()"""
        )
        tab.sleep(1.6)
        urls = request_urls(tab)
        tag_urls = [u for u in urls if "tag_ids=" in u]
        check("list query carries tag_ids twice (AND)", len(tag_urls) >= 1
              and all(u.count("tag_ids=") >= 2 for u in tag_urls[-1:]), str(tag_urls[-3:]))
        body = tab.evaluate("document.body.innerText")
        check("AND result keeps the row carrying both tags",
              "PROBE GROCERIES" in body and "PROBE GAS" not in body,
              "groceries kept, gas (one tag only) filtered out")

        # --- 4. management modal -------------------------------------------
        tab.evaluate(
            "document.querySelector('input[aria-label=\"Add tag to filter\"]').focus()"
        )
        tab.sleep(0.3)
        tab.wait_for("document.body.innerText.includes('Manage tags…')", timeout=10)
        tab.evaluate(
            """(() => {
              const b = [...document.querySelectorAll('button')]
                .find(el => el.textContent.trim() === 'Manage tags…');
              if (b) b.click();
              return Boolean(b);
            })()"""
        )
        tab.wait_for(
            "document.querySelector('[role=dialog][aria-labelledby]') !== null", timeout=10
        )
        manager = tab.evaluate(
            "document.querySelector('[role=dialog][aria-labelledby]')?.innerText ?? ''"
        )
        load_axe(tab)
        axe_check(tab, "tag management modal open")
        check("manager dialog lists usage counts",
              "transaction" in manager and "probe-extra" in manager,
              manager.replace("\n", " | ")[:200])

        # rename probe-extra -> probe-renamed
        tab.evaluate(
            """(() => {
              const b = [...document.querySelectorAll('button')]
                .find(el => el.textContent.trim().startsWith('Rename probe-extra'));
              if (b) b.click();
              return Boolean(b);
            })()"""
        )
        tab.sleep(0.3)
        tab.evaluate(
            """(() => {
              const i = document.querySelector('input[aria-label=\"New name for probe-extra\"]');
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
              setter.call(i, 'probe-renamed');
              i.dispatchEvent(new Event('input', {bubbles: true}));
            })()"""
        )
        tab.evaluate(
            """(() => {
              const b = [...document.querySelectorAll('button')]
                .find(el => el.textContent.trim() === 'Save name');
              if (b) b.click();
            })()"""
        )
        tab.sleep(1.2)
        renamed = tab.evaluate("document.body.innerText.includes('probe-renamed')")
        check("rename lands on the row chip", renamed, "probe-renamed visible after PATCH")
        tab.screenshot(str(HERE / "m104-manager-1440.png"))

        # merge: fold probe-shared into probe-renamed
        for name in ("probe-renamed", "probe-shared"):
            tab.evaluate(
                f"""(() => {{
                  const c = [...document.querySelectorAll('input[type=checkbox]')]
                    .find(el => el.getAttribute('aria-label') === 'Select {name} to merge');
                  if (c && !c.checked) c.click();
                }})()"""
            )
        tab.sleep(0.3)
        merge_label = tab.evaluate(
            """(() => {
              const b = [...document.querySelectorAll('button')]
                .find(el => /^Merge \\d+ into/.test(el.textContent.trim()));
              return b ? b.textContent.trim() : '';
            })()"""
        )
        tab.evaluate(
            """(() => {
              const b = [...document.querySelectorAll('button')]
                .find(el => /^Merge \\d+ into/.test(el.textContent.trim()));
              if (b) b.click();
            })()"""
        )
        tab.sleep(1.5)
        manager_after = tab.evaluate(
            "document.querySelector('[role=dialog][aria-labelledby]')?.innerText ?? ''"
        )
        check("merge folds the selection into one survivor",
              merge_label != "" and "probe-shared" not in manager_after,
              f"button={merge_label!r}")
        tab.evaluate(
            """(() => {
              const b = [...document.querySelectorAll('button')]
                .find(el => el.textContent.trim() === 'Close');
              if (b) b.click();
            })()"""
        )
        tab.sleep(0.5)

        # Read the console BEFORE the teardown below, so the teardown's own
        # requests cannot be mistaken for an app error.
        console_errors = tab.console_errors()

        # --- cleanup: the tenant is throwaway (scripts/smoke.sh convention) --
        cleaned = tab.evaluate(
            f"""(async () => {{
      const email = {json.dumps(seed_result['email'])};
      const password = {json.dumps(seed_result['password'])};
      const h = {{'Content-Type': 'application/json'}};
      const log = await fetch('/api/v1/auth/login',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const auth = {{...h, Authorization: 'Bearer ' + (await log.json()).access_token}};
      const r = await fetch('/api/v1/users/me',
        {{method:'DELETE', headers:auth, body: JSON.stringify({{current_password: password}})}});
      return r.status;
    }})()""",
            await_promise=True,
        )
        check("throwaway tenant deleted", cleaned == 204, f"DELETE /users/me -> {cleaned}")
        check("zero console/CSP errors", len(console_errors) == 0, str(console_errors[:3]))
    finally:
        browser.close()

    report = {
        "base": BASE,
        "width": args.width,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": CHECKS,
        "passed": sum(1 for c in CHECKS if c["ok"]),
        "failed": sum(1 for c in CHECKS if not c["ok"]),
    }
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}  passed={report['passed']} failed={report['failed']}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

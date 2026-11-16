"""M143 — the dual-market collision chooser + the unresolved row.

    A11Y_BASE=https://fintrac.princessandmark.com \
      python3 m143_symbol_collision_probe.py [--keep-tenant]

The deployed-stack half of AGENTS §4 step 5 for docs/07 §Candidate resolution
+ §Unresolved symbols and docs/09 §New security. A throwaway tenant is
registered OUTSIDE the page (no password ever touches a form) and its
httpOnly refresh cookie is injected, so every check below is the real app
against the real Yahoo seam:

1. **A collision is a choice.** `VGRO` answers BOTH a real US ETF
   (Virtus Silvant, NYSEArca, USD) and Vanguard's `VGRO.TO` (Toronto, CAD),
   so the form must render the pick-one chooser listing BOTH with their
   currencies — never a silently chosen bare-first listing (security 294).
2. **One lookup per typed string.** Picking a row applies it DIRECTLY:
   exactly one `/securities/lookup` request for `VGRO`, the chooser never
   re-opens, and `Create security` stores `VGRO.TO` / CAD / TSX.
3. **No UX tax on a single market.** `RY.TO` has one candidate — no chooser,
   no chip, the explicit Look up autofills.
4. **No synthesized identity.** A string nothing resolves to leaves the
   Currency field EMPTY (the old `"CAD"` default is gone); the M141 gate's
   "Add anyway" creates an UNRESOLVED row — typed name, `listing_currency`
   empty, `dead_symbol` true, no invented venue.
5. **a11y**: axe-core 0 critical/serious + console 0 errors, at 1440 and 390.

The tenant is deleted at the end (`DELETE /users/me` → 204) unless
`--keep-tenant` is passed. The demo account is never touched.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from a11y_driver import Browser
from axe_scan import force_viewport, load_axe, run_axe, settle, summarize
from probe_target import random_password

BASE = os.environ.get("A11Y_BASE", "https://fintrac.princessandmark.com").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))

FAILURES: list[str] = []
_CLEANUP: dict[str, Any] = {}

SYMBOL_INPUT = 'input[placeholder="XEQT"]'


def check(label: str, ok: bool, detail: Any = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail not in ("", None) else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")
    return ok


def note(label: str, detail: Any = "") -> None:
    print(f"  [note] {label}" + (f" — {detail}" if detail not in ("", None) else ""))


class Api:
    """HTTP outside the page: register/login/seeding never touch a form."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.cookie: str | None = None

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        url = f"{BASE}/api/v1{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read().decode()
                for key, value in resp.headers.items():
                    if key.lower() == "set-cookie" and value.startswith("refresh_token="):
                        self.cookie = value.split(";", 1)[0]
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:  # noqa: PERF203 — probe, not hot path
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw


def _delete_tenant() -> None:
    api = _CLEANUP.get("api")
    if not isinstance(api, Api) or _CLEANUP.get("done") or _CLEANUP.get("keep"):
        return
    status, _ = api.call("DELETE", "/users/me", {"current_password": _CLEANUP["password"]})
    _CLEANUP["done"] = True
    print(
        f"  [{'PASS' if status == 204 else 'FAIL'}] throwaway tenant deleted "
        f"({_CLEANUP.get('email')}) — DELETE /users/me {status}"
    )


atexit.register(_delete_tenant)


def register_tenant(api: Api) -> dict:
    password = random_password("m143")
    email = f"m143-probe-{int(time.time())}@example.com"
    status, _ = api.call("POST", "/auth/register", {"email": email, "password": password})
    check("register the throwaway tenant", status == 200, status)
    status, body = api.call("POST", "/auth/login", {"email": email, "password": password})
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise SystemExit(f"login failed: {status} {body}")
    api.token = body["access_token"]
    _CLEANUP.update({"api": api, "password": password, "email": email})
    check("login (no password touches a form)", True)
    return {"email": email}


def inject_session(tab, api: Api) -> None:
    tab.send("Network.enable")
    tab.send(
        "Network.setCookie",
        {
            "name": "refresh_token",
            "value": api.cookie.split("=", 1)[1] if api.cookie else "",
            "url": BASE,
            "httpOnly": True,
            "path": "/",
        },
    )


def field(tab, key: str) -> str:
    return str(tab.evaluate(f"document.querySelector('{SYMBOL_INPUT}')?.{key} ?? ''") or "")


def set_symbol(tab, text: str) -> None:
    """Write the field the way a typing user does (React sees the input)."""
    return tab.evaluate(
        "((text) => {"
        f"  const el = document.querySelector('{SYMBOL_INPUT}');"
        "  if (!el) return 'no field';"
        "  el.focus();"
        "  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;"
        "  setter.call(el, text);"
        "  el.dispatchEvent(new Event('input', { bubbles: true }));"
        "  return el.value;"
        f"}})({json.dumps(text)})"
    )


def open_form(tab) -> None:
    """Open the add form — idempotent (the toggle HIDES an open form)."""
    if not has(tab, SYMBOL_INPUT):
        tab.evaluate(
            "Array.from(document.querySelectorAll('button'))"
            "  .find(b => b.textContent.trim() === '+ New security')?.click()"
        )
    tab.wait_for(f"!!document.querySelector('{SYMBOL_INPUT}')", timeout=15)


def set_labeled(tab, label: str, value: str) -> None:
    tab.evaluate(
        "((args) => {"
        "  const el = Array.from(document.querySelectorAll('label'))"
        "    .find(l => l.textContent.trim().startsWith(args.label))"
        "    ?.querySelector('input');"
        "  if (!el) return 'no field';"
        "  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;"
        "  setter.call(el, args.value);"
        "  el.dispatchEvent(new Event('input', { bubbles: true }));"
        "  return el.value;"
        f"}})({json.dumps({'label': label, 'value': value})})"
    )


def form_fields(tab) -> dict[str, str]:
    out: dict[str, str] = {"symbol": field(tab, "value")}
    for label, key in (("Name", "name"), ("Currency", "currency"), ("Exchange", "exchange")):
        out[key] = str(
            tab.evaluate(
                "((label) => {"
                "  const el = Array.from(document.querySelectorAll('label'))"
                "    .find(l => l.textContent.trim().startsWith(label))"
                "    ?.querySelector('input');"
                "  return el ? el.value : '';"
                f"}})({json.dumps(label)})"
            )
            or ""
        )
    return out


def has(tab, selector: str) -> bool:
    return bool(tab.evaluate(f"!!document.querySelector({json.dumps(selector)})"))


def wait_field(tab, key: str, want: str, timeout: float = 20.0) -> str:
    """Poll a form field until it holds `want` (or the deadline passes).

    The `Look up` autofill is a NETWORK round-trip: reading the fields right
    after the click sees the pre-lookup state — that race is what made the
    first committed run report `currency: ''` for `RY.TO`.
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = form_fields(tab).get(key, "")
        if last == want:
            return last
        tab.sleep(0.4)
    return last


def reset_form(tab) -> None:
    """Close and reopen the add form so it starts from EMPTY_SECURITY.

    The form is unmounted when hidden (`showNewSecurity && ...`), so this is
    a genuine state reset — without it a late autofill from the previous
    step lands in the next step's fields and reads as a guessed default.
    """
    if has(tab, SYMBOL_INPUT):
        tab.evaluate(
            "Array.from(document.querySelectorAll('button'))"
            "  .find(b => b.textContent.trim() === 'Hide new-security form')?.click()"
        )
        tab.sleep(0.5)
    open_form(tab)
    tab.sleep(0.4)


def text_of(tab, selector: str) -> str:
    return str(tab.evaluate(f"document.querySelector({json.dumps(selector)})?.innerText ?? ''") or "")


def click_button(tab, label: str) -> str:
    return str(
        tab.evaluate(
            "((label) => {"
            "  const b = Array.from(document.querySelectorAll('button'))"
            "    .find(b => b.textContent.trim() === label);"
            "  if (!b) return 'no button';"
            "  b.click();"
            "  return b.disabled ? 'disabled' : 'clicked';"
            f"}})({json.dumps(label)})"
        )
        or ""
    )


def lookup_calls(tab) -> int:
    """How many `/securities/lookup` requests the page has made so far."""
    return int(
        tab.evaluate(
            "performance.getEntriesByType('resource')"
            "  .filter(e => e.name.includes('/securities/lookup')).length"
        )
        or 0
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-tenant", action="store_true")
    args = parser.parse_args()
    from probe_target import require_target
    require_target(BASE, who="m143_symbol_collision_probe.py")
    _CLEANUP["keep"] = args.keep_tenant

    api = Api()
    tenant = register_tenant(api)

    report: dict[str, Any] = {
        "base": BASE,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tenant": tenant["email"],
        "checks": [],
    }

    browser = Browser(width=1440, height=900)
    try:
        tab = browser.new_tab(f"{BASE}/login")
        tab.enable_runtime()
        force_viewport(tab, 1440, 900)
        tab.navigate(f"{BASE}/login")
        settle(tab, extra=0.8)
        inject_session(tab, api)
        tab.navigate(f"{BASE}/investments")
        tab.wait_for("location.pathname === '/investments'", timeout=25)
        settle(tab)
        check("session injected as the httpOnly cookie → /investments", True)

        # --- 1. a collision is a choice ------------------------------------------
        print("\n1. `VGRO` collides across markets → the chooser, both sides")
        open_form(tab)
        before = lookup_calls(tab)
        set_symbol(tab, "VGRO")
        found = tab.wait_for(
            f"!!document.querySelector('[data-testid=\"symbol-candidates\"]')", timeout=25
        )
        check("the collision opens the pick-one chooser", bool(found))
        chooser_text = text_of(tab, '[data-testid="symbol-candidates"]')
        note("chooser copy", chooser_text.replace("\n", " | ")[:220])
        check(
            "both markets are named",
            "VGRO" in chooser_text and "VGRO.TO" in chooser_text,
        )
        check(
            "each side shows its CURRENCY (the choice itself)",
            "USD" in chooser_text and "CAD" in chooser_text,
            chooser_text.replace("\n", " | ")[:220],
        )
        check(
            "the US side names Virtus, the CAD side Vanguard",
            "Virtus" in chooser_text and "VANGUARD" in chooser_text.upper(),
        )
        check("no chip while the choice is open", not has(tab, '[data-testid="symbol-suggestion"]'))
        check("the typed string is still what the field holds", field(tab, "value") == "VGRO")
        check("nothing was created by the offer alone", _securities(api) == [])
        report["chooser_text"] = chooser_text

        tab.screenshot(os.path.join(HERE, "m143-chooser-1440.png"))
        load_axe(tab)
        axe_summary = summarize((run_axe(tab) or {}).get("violations", []))
        check(
            "axe-core 0 critical/serious on the chooser",
            axe_summary["counts"]["critical"] == 0 and axe_summary["counts"]["serious"] == 0,
            axe_summary["counts"],
        )
        check("console 0 errors", tab.console_errors() == [], tab.console_errors()[:3])
        report["axe_1440"] = axe_summary

        # --- 2. one lookup per typed string; the pick is applied directly ---------
        print("\n2. Picking VGRO.TO applies it directly (no 2nd lookup)")
        tab.evaluate("document.querySelector('[data-testid=\"symbol-candidate-VGRO.TO\"]')?.click()")
        check(
            "the pick lands in the field",
            bool(tab.wait_for(f"document.querySelector('{SYMBOL_INPUT}').value === 'VGRO.TO'", timeout=20)),
        )
        filled = form_fields(tab)
        note("form after the pick", filled)
        check("name is Vanguard's", filled["name"] == "VANGUARD GROWTH ETF PORTFOLIO", filled["name"])
        check("currency CAD", filled["currency"] == "CAD")
        check("exchange TSX", filled["exchange"] == "TSX")
        check("the chooser closed", not has(tab, '[data-testid="symbol-candidates"]'))
        check(
            "exactly ONE lookup for the whole interaction",
            lookup_calls(tab) - before == 1,
            lookup_calls(tab) - before,
        )
        tab.screenshot(os.path.join(HERE, "m143-filled-1440.png"))

        check("Create security button clicked", click_button(tab, "Create security") == "clicked")
        check(
            "Create security stores VGRO.TO (CAD), not the bare ticker",
            _wait_for_security(api, "VGRO.TO"),
        )
        row = [r for r in _securities(api) if r.get("symbol") == "VGRO.TO"]
        if row:
            check("stored listing_currency is CAD", row[0].get("listing_currency") == "CAD")
            check("stored name is Vanguard's", row[0].get("name") == "VANGUARD GROWTH ETF PORTFOLIO")

        # --- 3. single-market symbols pay no chooser tax --------------------------
        print("\n3. `RY.TO` — one market, no chooser")
        reset_form(tab)
        set_symbol(tab, "RY.TO")
        tab.sleep(5.0)  # debounce + a real lookup round-trip
        check("no chooser for a single-market symbol", not has(tab, '[data-testid="symbol-candidates"]'))
        check("no chip either", not has(tab, '[data-testid="symbol-suggestion"]'))
        clicked = click_button(tab, "Look up") == "clicked"
        # The autofill is a network round-trip: read the fields WHEN THEY LAND.
        landed = wait_field(tab, "currency", "CAD", timeout=25.0)
        note("form after Look up", form_fields(tab))
        check(
            "Look up autofills without asking",
            clicked and landed == "CAD",
            f"clicked={clicked} currency={landed!r}",
        )
        check("currency CAD from the resolved listing", landed == "CAD", landed)
        check(
            "the autofill names the listing (no renamed symbol)",
            "ROYAL" in form_fields(tab)["name"].upper(),
            form_fields(tab)["name"],
        )

        # --- 4. no synthesized identity -------------------------------------------
        print("\n4. A string nothing resolves to → an UNRESOLVED row")
        # A FRESH form: the previous step's autofill must not leak in and read
        # as a guessed default (that is exactly how "VFV, USD" was born).
        reset_form(tab)
        fresh = form_fields(tab)
        note("fresh form", fresh)
        check("the form starts EMPTY — no guessed currency", fresh["currency"] == "", fresh)
        set_symbol(tab, "PRIVCONOTLISTED")
        tab.sleep(5.0)
        check("no chip", not has(tab, '[data-testid="symbol-suggestion"]'))
        check("no chooser", not has(tab, '[data-testid="symbol-candidates"]'))
        empty_currency = form_fields(tab)["currency"]
        check(
            "the Currency field is NOT pre-filled with a guess",
            empty_currency == "",
            repr(empty_currency),
        )
        set_labeled(tab, "Name", "PRIVCONOTLISTED")
        ready = form_fields(tab)
        note("form before submit", ready)
        clicked = click_button(tab, "Create security")
        try:
            gate = bool(
                tab.wait_for('!!document.querySelector(\'[data-testid="quote-warning"]\')', timeout=30)
            )
        except TimeoutError as exc:  # keep the rest of the run (a11y) alive
            gate = False
            note("gate wait", str(exc)[:180])
        check("M141's no-quote gate appears", gate, f"button={clicked} fields={ready}")
        check("Add anyway button clicked", click_button(tab, "Add anyway") == "clicked")
        check(
            "the row is created UNRESOLVED (empty currency, dead_symbol)",
            _wait_for_security(api, "PRIVCONOTLISTED"),
        )
        unresolved = [r for r in _securities(api) if r.get("symbol") == "PRIVCONOTLISTED"]
        if unresolved:
            row0 = unresolved[0]
            note("unresolved row", row0)
            check("listing_currency is empty", row0.get("listing_currency") == "", row0.get("listing_currency"))
            check("dead_symbol is set", row0.get("dead_symbol") is True, row0.get("dead_symbol"))
            check("name is the typed string", row0.get("name") == "PRIVCONOTLISTED")
        tab.screenshot(os.path.join(HERE, "m143-unresolved-1440.png"))

        # --- 5. a11y at the narrow width ------------------------------------------
        print("\n5. Narrow viewport (390)")
        force_viewport(tab, 390, 844)
        settle(tab, extra=0.6)
        axe_narrow = summarize((run_axe(tab) or {}).get("violations", []))
        check(
            "axe-core 0 critical/serious at 390",
            axe_narrow["counts"]["critical"] == 0 and axe_narrow["counts"]["serious"] == 0,
            axe_narrow["counts"],
        )
        tab.screenshot(os.path.join(HERE, "m143-form-390.png"))
        check("console 0 errors", tab.console_errors() == [], tab.console_errors()[:3])
        report["axe_390"] = axe_narrow

        browser.close()
    finally:
        report["failures"] = FAILURES
        with open(os.path.join(HERE, "m143-collision-probe.json"), "w") as handle:
            json.dump(report, handle, indent=2, default=str)
        print(f"\n{'ALL CHECKS PASSED' if not FAILURES else 'FAILURES: ' + str(FAILURES)}")
        print(f"report → {os.path.join(HERE, 'm143-collision-probe.json')}")
    return 0 if not FAILURES else 1


def _securities(api: Api) -> list[dict]:
    status, body = api.call("GET", "/securities?limit=200")
    if status != 200 or not isinstance(body, dict):
        return []
    return list(body.get("items") or [])


def _wait_for_security(api: Api, symbol: str, timeout: float = 40.0) -> bool:
    """The create queues a backfill; the row itself is visible immediately."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(row.get("symbol") == symbol for row in _securities(api)):
            return True
        time.sleep(1.0)
    return False


if __name__ == "__main__":
    sys.exit(main())

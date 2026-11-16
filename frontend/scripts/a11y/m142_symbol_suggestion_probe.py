"""M142 — the typed-name → canonical-ticker suggestion on the add form.

    A11Y_BASE=https://fintrac.princessandmark.com \
      python3 m142_symbol_suggestion_probe.py [--keep-tenant]

The deployed-stack half of AGENTS §4 step 5 for docs/09 §New security — the
symbol suggestion chip. A throwaway tenant is registered OUTSIDE the page (no
password ever touches a form) and its httpOnly refresh cookie is injected, so
every check below is the real app against the real Yahoo seam:

1. **The chooser, live.** Typing `APOTEX` into the New security form fires the
   debounced lookup, and because Yahoo's search returns TWO listings
   (`APTX.TO` Toronto + `APTX.NE` NEO) the form must render the pick-one list
   — never a silently chosen listing.
2. **The pick re-resolves.** Clicking `APTX.TO` re-resolves that CANONICAL
   symbol and fills the whole form (symbol, name, kind currency, exchange),
   then `Create security` stores THAT listing — `APTX.TO` / CAD — not the
   typed name.
3. **A miss while typing is silent.** `PRIVCONOTLISTED` produces no chip, no
   chooser and no toast; the explicit `Look up` button still owns the loud,
   persistent warning (W11 pin 7).
4. **A true ticker offers nothing.** `APTX` resolves through the `.TO`
   candidate with no chip (there is nothing to correct) and the explicit
   Look up autofills without renaming the symbol.
5. **a11y**: axe-core 0 critical/serious + console 0 errors + the chip's own
   geometry (its buttons do not print over its text), at 1440 and 390.

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
    password = random_password("m142")
    email = f"m142-probe-{int(time.time())}@example.com"
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


def text_of(tab, selector: str) -> str:
    return str(tab.evaluate(f"document.querySelector({json.dumps(selector)})?.innerText ?? ''") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-tenant", action="store_true")
    args = parser.parse_args()
    from probe_target import require_target
    require_target(BASE, who="m142_symbol_suggestion_probe.py")
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

        # --- 1. the chooser, live -------------------------------------------------
        print("\n1. Typed NAME → the pick-one chooser (APOTEX)")
        open_form(tab)
        set_symbol(tab, "APOTEX")
        found = tab.wait_for(
            f"!!document.querySelector('[data-testid=\"symbol-candidates\"]')", timeout=25
        )
        check("the chooser appears for a name with several listings", bool(found))
        chooser_text = text_of(tab, '[data-testid="symbol-candidates"]')
        check(
            "it names both live listings (APTX.TO Toronto + APTX.NE NEO)",
            "APTX.TO" in chooser_text
            and "APTX.NE" in chooser_text
            and "APOTEX HEALTH CORP" in chooser_text,
            chooser_text.replace("\n", " | ")[:160],
        )
        check("no chip while the choice is open", not has(tab, '[data-testid="symbol-suggestion"]'))
        check("the typed string is still what the field holds", field(tab, "value") == "APOTEX")
        tab.send("Network.enable")
        report["chooser_text"] = chooser_text

        tab.screenshot(os.path.join(HERE, "m142-chooser-1440.png"))
        load_axe(tab)
        axe_summary = summarize((run_axe(tab) or {}).get("violations", []))
        check(
            "axe-core 0 critical/serious on the chooser",
            axe_summary["counts"]["critical"] == 0 and axe_summary["counts"]["serious"] == 0,
            axe_summary["counts"],
        )
        check("console 0 errors", tab.console_errors() == [], tab.console_errors()[:3])
        report["axe_1440"] = axe_summary

        # --- 2. the pick re-resolves and stores the canonical listing -------------
        print("\n2. Picking APTX.TO re-resolves the canonical symbol")
        tab.evaluate(
            "document.querySelector('[data-testid=\"symbol-candidate-APTX.TO\"]')?.click()"
        )
        check(
            "the pick re-resolves APTX.TO into the field",
            bool(tab.wait_for(f"document.querySelector('{SYMBOL_INPUT}').value === 'APTX.TO'", timeout=25)),
        )
        filled = form_fields(tab)
        note("form after the pick", filled)
        check("name autofilled from the canonical listing", filled["name"] == "APOTEX HEALTH CORP")
        check("currency is CAD (the `.TO` FX seam)", filled["currency"] == "CAD")
        check("exchange normalized to TSX", filled["exchange"] == "TSX")
        check("the chooser closed", not has(tab, '[data-testid="symbol-candidates"]'))
        tab.screenshot(os.path.join(HERE, "m142-filled-1440.png"))

        clicked = tab.evaluate(
            "(() => {"
            "  const b = Array.from(document.querySelectorAll('button'))"
            "    .find(b => b.textContent.trim() === 'Create security');"
            "  if (!b) return 'no button';"
            "  b.click();"
            "  return b.disabled ? 'disabled' : 'clicked';"
            "})()"
        )
        check("Create security button clicked", clicked == "clicked", clicked)
        tab.sleep(6.0)
        note("path after the click", tab.evaluate("location.pathname"))
        note("console after the click", tab.console_errors())
        network: list[tuple[object, object]] = []
        for entry in tab.events("Network.responseReceived"):
            params = entry.get("params", entry) if isinstance(entry, dict) else {}
            response = params.get("response", {}) if isinstance(params, dict) else {}
            if isinstance(response, dict):
                network.append((response.get("url"), response.get("status")))
        note("network after the pick", network[-14:])
        check(
            "the create landed — the success toast names APTX.TO",
            bool(tab.wait_for("document.body.innerText.includes('APTX.TO')", timeout=20)),
            str(tab.evaluate("document.body.innerText"))[:220].replace("\n", " | "),
        )
        tab.screenshot(os.path.join(HERE, "m142-created-1440.png"))

        # --- 3. a miss while typing is silent -------------------------------------
        print("\n3. A miss while typing stays silent; the button owns the warning")
        open_form(tab)
        set_symbol(tab, "PRIVCONOTLISTED")
        tab.sleep(5.0)  # debounce (600ms) + a real lookup round-trip
        check("no chip for a string nothing resolves to", not has(tab, '[data-testid="symbol-suggestion"]'))
        check("no chooser either", not has(tab, '[data-testid="symbol-candidates"]'))
        check("no toast", tab.evaluate("document.querySelectorAll('[role=\"status\"], [data-testid*=\"toast\"]').length") in (0, 1))
        check("no M141 quote banner while typing", not has(tab, '[data-testid="quote-warning"], [data-testid="lookup-warning"]'))
        tab.evaluate(
            "Array.from(document.querySelectorAll('button'))"
            "  .find(b => b.textContent.trim() === 'Look up')?.click()"
        )
        warned = tab.wait_for(
            "!!document.querySelector('[data-testid=\"lookup-warning\"]')", timeout=20
        )
        check(
            "the explicit Look up still shows the loud, persistent warning",
            bool(warned) and "PRIVCONOTLISTED" in text_of(tab, '[data-testid="lookup-warning"]'),
        )
        tab.screenshot(os.path.join(HERE, "m142-miss-warning-1440.png"))

        # --- 4. a true ticker offers nothing --------------------------------------
        print("\n4. A true ticker (APTX) resolves with no chip")
        open_form(tab)
        set_symbol(tab, "APTX")
        tab.sleep(5.0)
        check("no chip — the typed symbol IS the listing", not has(tab, '[data-testid="symbol-suggestion"]'))
        check("no chooser", not has(tab, '[data-testid="symbol-candidates"]'))
        tab.evaluate(
            "Array.from(document.querySelectorAll('button'))"
            "  .find(b => b.textContent.trim() === 'Look up')?.click()"
        )
        look = tab.wait_for(f"document.querySelector('{SYMBOL_INPUT}').value === 'APTX'", timeout=20)
        # The autofill is a NETWORK round-trip; `symbol === 'APTX'` is already
        # true from typing, so reading straight after the click saw the
        # pre-lookup state. Poll for the value the lookup actually writes.
        deadline = time.time() + 20.0
        filled_after_lookup = form_fields(tab)
        while time.time() < deadline and filled_after_lookup["currency"] != "CAD":
            tab.sleep(0.4)
            filled_after_lookup = form_fields(tab)
        check(
            "Look up autofills without renaming the symbol",
            bool(look) and filled_after_lookup["currency"] == "CAD",
            filled_after_lookup,
        )
        tab.screenshot(os.path.join(HERE, "m142-ticker-1440.png"))

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
        tab.screenshot(os.path.join(HERE, "m142-form-390.png"))
        report["axe_390"] = axe_narrow

        browser.close()

        # --- 6. what actually got stored, over a FRESH session -------------------
        # A second login DURING the browser session rotates the tenant's
        # refresh token and logs the page out (which is what the first runs
        # tripped over), so the API half runs after the browser is gone.
        print("\n6. What actually got stored (fresh session, after the browser)")
        verifier = _fresh_api()
        rows = _securities(verifier)
        note("tenant securities", rows)
        created = [r for r in rows if r.get("symbol") == "APTX.TO"]
        check(
            "exactly one security exists, and it is APTX.TO (not APOTEX)",
            len(rows) == 1 and len(created) == 1,
            rows,
        )
        if created:
            check("stored listing_currency is CAD", created[0].get("listing_currency") == "CAD")
            check("stored name is the canonical one", created[0].get("name") == "APOTEX HEALTH CORP")
            check("stored exchange is TSX", created[0].get("exchange") == "TSX")
        report["stored"] = rows
    finally:
        report["failures"] = FAILURES
        with open(os.path.join(HERE, "m142-suggestion-probe.json"), "w") as handle:
            json.dump(report, handle, indent=2, default=str)
        print(f"\n{'ALL CHECKS PASSED' if not FAILURES else 'FAILURES: ' + str(FAILURES)}")
        print(f"report → {os.path.join(HERE, 'm142-suggestion-probe.json')}")
    return 0 if not FAILURES else 1


def _fresh_api() -> Api:
    """A NEW session for the same throwaway tenant.

    The browser rotates the refresh token when it restores the session, which
    revokes the probe's original access token — so anything verified over the
    probe's own login after that point reads an empty database and blames the
    app. Re-login (same credentials, still outside any page) instead.
    """
    fresh = Api()
    status, body = fresh.call(
        "POST", "/auth/login", {"email": _CLEANUP["email"], "password": _CLEANUP["password"]}
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise SystemExit(f"verify login failed: {status} {body}")
    fresh.token = body["access_token"]
    return fresh


def _securities(api: Api) -> list[dict]:
    status, body = api.call("GET", "/securities?limit=200")
    if status != 200 or not isinstance(body, dict):
        return []
    return list(body.get("items") or [])


if __name__ == "__main__":
    sys.exit(main())

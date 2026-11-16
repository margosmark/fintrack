"""M186 — the watchlist picker's market lookup fallback.

    A11Y_BASE=http://localhost:8080 \
      python3 m186_watchlist_market_probe.py [--keep-tenant]

The running-stack half of AGENTS §4 step 5 for docs/09 §Watchlist ticker
search — the market lookup fallback. A throwaway tenant is registered OUTSIDE
the page (no password ever touches a form) and its httpOnly refresh cookie is
injected, so every check below is the real app against the real Yahoo seam:

1. **A stored match short-circuits the market.** With `RY.TO` seeded into the
   tenant, typing `ry` offers the local row and NEVER shows
   `security-market-searching` or a `security-market-option-*` row.
2. **An unknown ticker reaches the market.** Typing `cour` (nothing stored
   matches) fires the 600 ms probe and offers
   `security-market-option-COUR` — symbol, venue, currency and the
   `· from market` tail — while the no-match copy stays away.
3. **The pick IS the write.** Clicking that row creates the security
   (`POST /securities`) and watches it (`POST /watchlist`): the field clears,
   `watch-row-COUR` appears, the toast names COUR — and a FRESH session
   verifies both rows really landed.
4. **A miss stays honest.** `PRIVCONOTLISTED` keeps
   "No matching securities." — no row, no create, no watch.
5. **a11y**: axe-core 0 critical/serious with the listbox open + console 0
   errors, at 1440 and 390.

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

BASE = os.environ.get("A11Y_BASE", "http://localhost:8080").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))

FAILURES: list[str] = []
_CLEANUP: dict[str, Any] = {}

WATCH_INPUT = 'input[placeholder="Add a symbol to follow…"]'
# A miss is a STATE (docs/09): the same string the M142 probe proves silent.
MISS_STRING = "PRIVCONOTLISTED"


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
    password = random_password("m186")
    email = f"m186-probe-{int(time.time())}@example.com"
    status, _ = api.call("POST", "/auth/register", {"email": email, "password": password})
    check("register the throwaway tenant", status == 200, status)
    status, body = api.call("POST", "/auth/login", {"email": email, "password": password})
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise SystemExit(f"login failed: {status} {body}")
    api.token = body["access_token"]
    _CLEANUP.update({"api": api, "password": password, "email": email})
    check("login (no password touches a form)", True)
    return {"email": email}


def seed_stored_security(api: Api) -> dict | None:
    """The local-hit control: one STORED row the picker must find by itself."""
    status, body = api.call(
        "POST",
        "/securities",
        {
            "symbol": "RY.TO",
            "name": "Royal Bank of Canada",
            "kind": "stock",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
    )
    check("seed one stored security (RY.TO)", status == 201, f"{status} {body}")
    return body if isinstance(body, dict) else None


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


def has(tab, selector: str) -> bool:
    return bool(tab.evaluate(f"!!document.querySelector({json.dumps(selector)})"))


def text_of(tab, selector: str) -> str:
    return str(tab.evaluate(f"document.querySelector({json.dumps(selector)})?.innerText ?? ''") or "")


def set_query(tab, text: str) -> None:
    """Write the watchlist field the way a typing user does (React sees it)."""
    tab.evaluate(
        "((text) => {"
        f"  const el = document.querySelector({json.dumps(WATCH_INPUT)});"
        "  if (!el) return 'no field';"
        "  el.focus();"
        "  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;"
        "  setter.call(el, text);"
        "  el.dispatchEvent(new Event('input', { bubbles: true }));"
        "  return el.value;"
        f"}})({json.dumps(text)})"
    )


def field_value(tab) -> str:
    return str(tab.evaluate(f"document.querySelector({json.dumps(WATCH_INPUT)})?.value ?? ''") or "")


def click_market_row(tab, testid: str) -> bool:
    return bool(
        tab.evaluate(
            "((id) => {"
            "  const b = document.querySelector(`[data-testid=\"${id}\"]`);"
            "  if (!b) return false;"
            "  b.click();"
            "  return true;"
            f"}})({json.dumps(testid)})"
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-tenant", action="store_true")
    args = parser.parse_args()
    from probe_target import require_target
    require_target(BASE, who="m186_watchlist_market_probe.py")
    _CLEANUP["keep"] = args.keep_tenant

    api = Api()
    tenant = register_tenant(api)
    seed_stored_security(api)

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

        # --- 1. a stored match short-circuits the market -------------------------
        print("\n1. A stored match wins: the market is never asked")
        set_query(tab, "ry")
        local = tab.wait_for(
            '!!document.querySelector(\'[data-testid="security-option-RY.TO"]\')', timeout=15
        )
        check("the stored RY.TO row appears from the local list", bool(local))
        tab.sleep(2.5)  # comfortably past the 600 ms debounce
        check(
            "no pending row (the probe never fired)",
            not has(tab, '[data-testid="security-market-searching"]'),
        )
        check(
            "no market row either",
            not has(tab, '[data-testid^="security-market-option-"]'),
        )
        check("the no-match copy stays away (there IS a match)", not has(tab, '[data-testid="security-no-match"]'))
        tab.screenshot(os.path.join(HERE, "m186-local-1440.png"))

        # --- 2. an unknown ticker reaches the market -----------------------------
        print("\n2. An unknown ticker (cour) reaches the market")
        set_query(tab, "cour")
        # The pending row is a 600 ms + round-trip flash — catch it if we can.
        pending_seen = False
        deadline = time.time() + 2.0
        while time.time() < deadline and not pending_seen:
            pending_seen = has(tab, '[data-testid="security-market-searching"]')
            if not pending_seen:
                tab.sleep(0.05)
        note("pending row observed while the probe was out", pending_seen)
        market = tab.wait_for(
            '!!document.querySelector(\'[data-testid="security-market-option-COUR"]\')',
            timeout=25,
        )
        check("the market row appears for the stored-list miss", bool(market))
        label = text_of(tab, '[data-testid="security-market-option-COUR"]')
        note("market row label", label.replace("\n", " "))
        check(
            "it names symbol + name + venue + currency + the market tail",
            "COUR" in label
            and "Coursera" in label
            and "USD" in label
            and "from market" in label,
            label.replace("\n", " ")[:160],
        )
        check(
            "the no-match copy is withheld while the market row shows",
            not has(tab, '[data-testid="security-no-match"]'),
        )
        tab.screenshot(os.path.join(HERE, "m186-market-1440.png"))

        load_axe(tab)
        axe_summary = summarize((run_axe(tab) or {}).get("violations", []))
        check(
            "axe-core 0 critical/serious with the listbox open",
            axe_summary["counts"]["critical"] == 0 and axe_summary["counts"]["serious"] == 0,
            axe_summary["counts"],
        )
        check("console 0 errors", tab.console_errors() == [], tab.console_errors()[:3])
        report["axe_1440"] = axe_summary

        # --- 3. the pick is the write --------------------------------------------
        print("\n3. Picking the row creates the security and watches it")
        clicked = click_market_row(tab, "security-market-option-COUR")
        check("the market row was clicked", clicked)
        cleared = tab.wait_for(
            f"document.querySelector({json.dumps(WATCH_INPUT)})?.value === ''", timeout=15
        )
        check("the field cleared itself on the pick", bool(cleared))
        watched = tab.wait_for('!!document.querySelector(\'[data-testid="watch-row-COUR"]\')', timeout=25)
        check("watch-row-COUR appears in the Watchlist card", bool(watched))
        check(
            "the toast names COUR",
            bool(tab.wait_for("document.body.innerText.includes('COUR added.')", timeout=15)),
        )
        tab.screenshot(os.path.join(HERE, "m186-added-1440.png"))
        note("console after the pick", tab.console_errors())

        # --- 4. a miss stays honest -----------------------------------------------
        print("\n4. A miss keeps the honest no-match copy")
        set_query(tab, MISS_STRING)
        tab.sleep(5.0)  # debounce (600 ms) + a real lookup round-trip
        check(
            "no market row for a string nothing resolves to",
            not has(tab, '[data-testid^="security-market-option-"]'),
        )
        check(
            "no candidate rows either",
            not has(tab, '[data-testid^="security-market-candidate-"]'),
        )
        check(
            "the no-match copy keeps its exact words",
            has(tab, '[data-testid="security-no-match"]')
            and "No matching securities." in text_of(tab, '[data-testid="security-no-match"]'),
            text_of(tab, '[data-testid="security-no-match"]'),
        )
        check("still exactly one watched row (nothing was written)", has(tab, '[data-testid="watch-row-COUR"]'))
        tab.screenshot(os.path.join(HERE, "m186-miss-1440.png"))

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
        tab.screenshot(os.path.join(HERE, "m186-miss-390.png"))
        report["axe_390"] = axe_narrow

        browser.close()

        # --- 6. what actually got stored, over a FRESH session --------------------
        # A second login DURING the browser session rotates the refresh token,
        # so the API half runs after the browser is gone (M142's lesson).
        print("\n6. What actually got stored (fresh session, after the browser)")
        verifier = _fresh_api()
        securities = _list(verifier, "/securities?limit=200")
        watchlist = _list(verifier, "/watchlist")
        note("tenant securities", [(r.get("symbol"), r.get("listing_currency")) for r in securities])
        note("tenant watchlist", [r.get("symbol") for r in watchlist])
        check(
            "exactly two securities: the seeded RY.TO and the watched COUR",
            sorted(r.get("symbol") or "" for r in securities) == ["COUR", "RY.TO"],
            securities,
        )
        created = [r for r in securities if r.get("symbol") == "COUR"]
        if created:
            check("stored COUR listing_currency is USD (no guessed CAD)", created[0].get("listing_currency") == "USD")
            check("stored COUR name is the canonical one", created[0].get("name") == "Coursera, Inc.")
        check(
            "exactly ONE watchlist row, and it is COUR",
            len(watchlist) == 1 and (watchlist[0].get("symbol") if watchlist else None) == "COUR",
            watchlist,
        )
        check(
            "the seeded RY.TO is NOT watched (a local hit only selects)",
            not any(r.get("symbol") == "RY.TO" for r in watchlist),
        )
        report["stored_securities"] = securities
        report["stored_watchlist"] = watchlist
    finally:
        report["failures"] = FAILURES
        with open(os.path.join(HERE, "m186-watchlist-market-probe.json"), "w") as handle:
            json.dump(report, handle, indent=2, default=str)
        print(f"\n{'ALL CHECKS PASSED' if not FAILURES else 'FAILURES: ' + str(FAILURES)}")
        print(f"report → {os.path.join(HERE, 'm186-watchlist-market-probe.json')}")
    return 0 if not FAILURES else 1


def _fresh_api() -> Api:
    """A NEW session for the same throwaway tenant (the browser rotated its
    refresh token when it restored the session — see m142's probe)."""
    fresh = Api()
    status, body = fresh.call(
        "POST", "/auth/login", {"email": _CLEANUP["email"], "password": _CLEANUP["password"]}
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise SystemExit(f"verify login failed: {status} {body}")
    fresh.token = body["access_token"]
    return fresh


def _list(api: Api, path: str) -> list[dict]:
    status, body = api.call("GET", path)
    if status != 200 or not isinstance(body, dict):
        return []
    return list(body.get("items") or [])


if __name__ == "__main__":
    sys.exit(main())

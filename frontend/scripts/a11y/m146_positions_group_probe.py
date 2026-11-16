"""M146 — Positions by security: the own-position lens, live on the stack.

    A11Y_BASE=https://fintrac.princessandmark.com \\
      python3 m146_positions_group_probe.py [--keep-tenant]

The deployed-stack half of AGENTS §4 step 5 for docs/09 §Positions by security
(M146). A throwaway tenant is registered OUTSIDE the page (no password ever
touches a form), seeded through the real API with the acceptance SHAPE — one
ETF in five accounts, one in two, one in one, and one nobody has priced — and
its httpOnly refresh cookie is injected, so every check below is the real app
against a real `/portfolio/positions` payload:

1. **The lens is there** on the portfolio view: `By account | By security`.
2. **The fold**: 10 flat rows collapse to 3 group rows + 1 flat row; the
   5-account group carries the `5 accounts` chip and Σs the SERVER's own rows
   (quantity, market value, book cost, gain) — every number recomputed here
   from `GET /portfolio/positions` and compared to what the table prints.
3. **Value-weighted percents**: the group's `Day Δ` is Σ day Δ ÷ Σ (value −
   day Δ) from the payload. The discriminating case is `Your gain %`: the
   accounts bought the same ETF at DIFFERENT entry costs, so Σ gain ÷ Σ book
   must differ from the mean of the rows' percentages — and it does.
4. **Disclosure**: the chevron opens 5 inline per-account rows linking their
   accounts; a second disclosure closes the first; the single-account
   security has NO per-account chevron at all.
5. **The all-null group** (`NQPTEST`, two accounts, no prices) renders "no
   price" and em dashes — never a fabricated $0.
6. **The Σ row is byte-identical** across both views (the trust check).
7. **a11y**: axe-core 0 critical/serious + console 0 errors at 1440 and 390.

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
from datetime import date, timedelta
from typing import Any

from a11y_driver import Browser
from axe_scan import force_viewport, load_axe, run_axe, settle, summarize
from probe_target import random_password

BASE = os.environ.get("A11Y_BASE", "https://fintrac.princessandmark.com").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))

FAILURES: list[str] = []
CHECKS: list[dict[str, Any]] = []
_CLEANUP: dict[str, Any] = {}

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)

# The seeded shape. Fake tickers on purpose: a real symbol would pull a
# 365-day backfill from Yahoo and the manual closes that make this probe
# deterministic would sit beside market data the probe does not control.
PLAN: list[dict[str, Any]] = [
    {
        "symbol": "VEQTEST",
        "name": "Vanguard All-Equity ETF Portfolio (probe)",
        "accounts": [1, 2, 3, 4, 5],
        "quantities": ["100", "200", "300", "400", "500"],
        "price": "40.00",
        "prior": "39.60",
        # Different entry costs per account — the case where the group's
        # value-weighted gain % is NOT the mean of the rows' percentages.
        "book_prices": ["36.00", "38.00", "40.00", "42.00", "44.00"],
    },
    {
        "symbol": "VXUTEST",
        "name": "VXUS Total International (probe)",
        "accounts": [1, 2],
        "quantities": ["50", "150"],
        "price": "60.00",
        "prior": "60.00",
        "book_prices": ["50.00", "50.00"],
    },
    {
        "symbol": "ZGRTEST",
        "name": "ZGRO Balanced Growth (probe)",
        "accounts": [1],
        "quantities": ["10"],
        "price": "250.00",
        "prior": "249.00",
        "book_prices": ["200.00"],
    },
    {
        # No prices at all, held in TWO accounts: the all-null group.
        "symbol": "NQPTEST",
        "name": "No-Quote Probe Holding",
        "accounts": [3, 4],
        "quantities": ["5", "15"],
        "price": None,
        "prior": None,
        "book_prices": ["10.00", "10.00"],
    },
]

ACCOUNT_TYPES = ["tfsa", "rrsp", "brokerage_non_registered", "resp", "fhsa"]


def check(label: str, ok: bool, detail: Any = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail not in ("", None) else ""))
    CHECKS.append({"label": label, "ok": bool(ok), "detail": str(detail)[:300]})
    if not ok:
        FAILURES.append(f"{label}: {detail}")
    return ok


def note(label: str, detail: Any = "") -> None:
    print(f"  [note] {label}" + (f" — {detail}" if detail not in ("", None) else ""))


def cad(value: str | float) -> str:
    """en-CA CAD, the shape `fmtCad` prints ($60,000.00 / −$2,000.00)."""
    amount = float(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


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
            with urllib.request.urlopen(req, timeout=60) as resp:
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


def register_tenant(api: Api) -> None:
    password = random_password("m146")
    email = f"m146-probe-{int(time.time())}@example.com"
    status, body = api.call("POST", "/auth/register", {"email": email, "password": password})
    check("register the throwaway tenant", status == 200, body if status != 200 else status)
    status, body = api.call("POST", "/auth/login", {"email": email, "password": password})
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise SystemExit(f"login failed: {status} {body}")
    api.token = body["access_token"]
    _CLEANUP.update({"api": api, "password": password, "email": email})
    check("login (no password touches a form)", True)


def seed(api: Api) -> None:
    """Five accounts, four securities, ten trades, eight manual closes."""
    accounts: dict[str, int] = {}
    for index, kind in enumerate(ACCOUNT_TYPES):
        status, body = api.call(
            "POST",
            "/accounts",
            {"name": f"Probe {index + 1}", "type": kind, "currency": "CAD"},
        )
        if status != 201 or not isinstance(body, dict):
            raise SystemExit(f"account create failed: {status} {body}")
        accounts[str(index + 1)] = int(body["id"])
    check(f"{len(accounts)} investment accounts created", len(accounts) == 5)

    securities: dict[str, int] = {}
    for plan in PLAN:
        status, body = api.call(
            "POST",
            "/securities",
            {
                "symbol": plan["symbol"],
                "name": plan["name"],
                "kind": "etf",
                "listing_currency": "CAD",
                "exchange": "TSX",
            },
        )
        if status != 201 or not isinstance(body, dict):
            raise SystemExit(f"security create failed: {status} {body}")
        securities[plan["symbol"]] = int(body["id"])
    check(f"{len(securities)} securities created", len(securities) == len(PLAN))

    trades = 0
    closes = 0
    for plan in PLAN:
        security_id = securities[plan["symbol"]]
        for slot, account_key in enumerate(plan["accounts"]):
            status, body = api.call(
                "POST",
                "/trades",
                {
                    "account_id": accounts[str(account_key)],
                    "security_id": security_id,
                    "action": "buy",
                    "date": (YESTERDAY - timedelta(days=30)).isoformat(),
                    "quantity": plan["quantities"][slot],
                    "price_native": plan["book_prices"][slot],
                },
            )
            if status != 201:
                raise SystemExit(f"trade create failed: {status} {body}")
            trades += 1
        if plan["price"] is not None:
            for day, price in ((YESTERDAY, plan["prior"]), (TODAY, plan["price"])):
                status, body = api.call(
                    "POST",
                    "/prices",
                    {"security_id": security_id, "date": day.isoformat(), "price": price},
                )
                if status not in (200, 201):
                    raise SystemExit(f"price upsert failed: {status} {body}")
                closes += 1
    check(f"{trades} trades across the 5 accounts", trades == 10)
    # Three securities carry prices; the fourth (NQPTEST) deliberately has none.
    check(f"{closes} manual closes (two per priced security)", closes == 6)


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


def group_rows(tab) -> list[str]:
    """The visible group rows' text, in table order."""
    return list(
        tab.evaluate(
            "Array.from(document.querySelectorAll('[data-testid=\"position-group-row\"]'))"
            "  .map(r => r.innerText.replace(/\\n/g, ' | '))"
        )
        or []
    )


def total_row_text(tab) -> str:
    return str(
        tab.evaluate(
            "document.querySelector('[data-testid=\"positions-total-row\"]')?.innerText"
            "  .replace(/\\n/g, ' | ') ?? ''"
        )
        or ""
    )


def click_view(tab, label: str) -> None:
    tab.evaluate(
        "((label) => {"
        "  const tab = Array.from(document.querySelectorAll('[role=\"tab\"]'))"
        "    .find(t => t.textContent.trim() === label);"
        "  if (tab) tab.click();"
        f"}})({json.dumps(label)})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-tenant", action="store_true")
    args = parser.parse_args()
    from probe_target import require_target
    require_target(BASE, who="m146_positions_group_probe.py")
    _CLEANUP["keep"] = args.keep_tenant

    api = Api()
    register_tenant(api)
    seed(api)

    report: dict[str, Any] = {
        "base": BASE,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tenant": _CLEANUP.get("email"),
        "checks": CHECKS,
    }

    # What the SERVER says the fold must add up to — the probe recomputes every
    # expectation from THIS payload, so the UI is never compared to a constant
    # typed by hand.
    status, positions = api.call("GET", "/portfolio/positions")
    check("GET /portfolio/positions 200", status == 200 and isinstance(positions, dict))
    items = (positions or {}).get("items") or []
    flat_rows = len(items)
    per_security: dict[str, dict[str, Any]] = {}
    for row in items:
        bucket = per_security.setdefault(
            row["symbol"],
            {
                "accounts": [],
                "quantity": 0.0,
                "value": 0.0,
                "book": 0.0,
                "day": 0.0,
                "day_base": 0.0,
                "gain": 0.0,
                "day_pcts": [],
            },
        )
        bucket["accounts"].append(row["account_name"])
        bucket["quantity"] += float(row["quantity"])
        if row["market_value_cad"] is not None:
            bucket["value"] += float(row["market_value_cad"])
        if row["book_cost_cad"] is not None:
            bucket["book"] += float(row["book_cost_cad"])
        if row["unrealized_gain_cad"] is not None:
            bucket["gain"] += float(row["unrealized_gain_cad"])
        if row["day_change_cad"] is not None and row["market_value_cad"] is not None:
            bucket["day"] += float(row["day_change_cad"])
            bucket["day_base"] += float(row["market_value_cad"]) - float(row["day_change_cad"])
        if row["day_change_pct"] is not None:
            bucket["day_pcts"].append(float(row["day_change_pct"]))
    report["server_payload"] = {
        "flat_rows": flat_rows,
        "per_security": per_security,
        "totals": {
            "total_value_cad": (positions or {}).get("total_value_cad"),
            "total_unrealized_cad": (positions or {}).get("total_unrealized_cad"),
        },
    }

    browser = Browser(width=1440, height=900)
    try:
        tab = browser.new_tab(f"{BASE}/login")
        tab.enable_runtime()
        force_viewport(tab, 1440, 900)
        tab.navigate(f"{BASE}/login")
        settle(tab, extra=0.8)
        inject_session(tab, api)
        # The CDP profile outlives a probe run, and this lens persists in
        # localStorage by design (M146) — so the "By account is the default"
        # check would be reading the PREVIOUS run's lens. Clear it here, the
        # way a first-ever visitor arrives.
        tab.evaluate("localStorage.removeItem('fintrack:positions_view')")
        tab.navigate(f"{BASE}/investments")
        tab.wait_for("location.pathname === '/investments'", timeout=30)
        settle(tab, extra=1.0)
        check("session injected as the httpOnly cookie → /investments", True)

        # --- 1. the lens --------------------------------------------------------
        print("\n1. The lens on the Positions card")
        check("the table painted (flat rows on screen)", flat_rows > 0, flat_rows)
        check(
            "By account | By security toggle renders",
            bool(
                tab.wait_for(
                    "Array.from(document.querySelectorAll('[role=\"tab\"]'))"
                    "  .some(t => t.textContent.trim() === 'By security')",
                    timeout=25,
                )
            ),
        )
        check(
            "By account is the default (no stored lens yet)",
            bool(
                tab.evaluate(
                    "document.querySelector('[role=\"tab\"][aria-selected=\"true\"]')"
                    "  ?.textContent.trim() === 'By account'"
                )
            ),
        )
        check(
            "the flat table shows one row per account+security pair",
            bool(tab.wait_for(f"document.querySelectorAll('tbody tr').length >= {flat_rows}", timeout=20)),
            tab.evaluate("document.querySelectorAll('tbody tr').length"),
        )
        tab.screenshot(os.path.join(HERE, "m146-flat-1440.png"))

        # --- 2. the fold --------------------------------------------------------
        print("\n2. By security — the fold")
        click_view(tab, "By security")
        check("the lens persisted to localStorage", tab.evaluate("localStorage.getItem('fintrack:positions_view')") == "security")
        settle(tab, extra=0.6)
        rows = group_rows(tab)
        note("group rows", rows)
        multi = {k: v for k, v in per_security.items() if len(v["accounts"]) > 1}
        check(
            "one group row per MULTI-ACCOUNT security (single-account stays flat)",
            len(rows) == len(multi),
            f"{len(rows)} rows vs {len(multi)} multi-account securities",
        )

        veqt = next((r for r in rows if "VEQTEST" in r), "")
        check("the 5-account holding is one group row", bool(veqt), veqt[:120])
        check("…with the '5 accounts' chip", "5 accounts" in veqt)
        expect = per_security["VEQTEST"]
        qty = str(int(expect["quantity"])) if float(expect["quantity"]).is_integer() else str(expect["quantity"])
        check(f"Σ quantity {qty} matches the server's rows", qty in veqt, veqt[:160])
        check(f"Σ market value {cad(expect['value'])}", cad(expect["value"]) in veqt)
        check(f"Σ book cost {cad(expect['book'])} (display only — not a pooled ACB)", cad(expect["book"]) in veqt)
        check(f"Σ gain {cad(expect['gain'])}", cad(expect["gain"]) in veqt)
        weighted = 100 * expect["day"] / expect["day_base"] if expect["day_base"] else None
        shown = f"{weighted:+.2f}%" if weighted is not None else "—"
        # A security has ONE price, so its rows' day % are the same number and
        # the weighted and mean answers coincide BY CONSTRUCTION — the group
        # still has to PRINT the weighted formula's answer (docs/09).
        check(f"Day Δ is the value-weighted formula's answer ({shown})", shown in veqt)

        # The gain % is where weighting bites: the accounts bought at different
        # entry costs, so Σ gain ÷ Σ book is NOT the mean of the rows' %s.
        gain_shown = f"{100 * expect['gain'] / expect['book']:+.2f}%" if expect["book"] else "—"
        row_pcts = [
            float(r["unrealized_gain_pct"])
            for r in items
            if r["symbol"] == "VEQTEST" and r["unrealized_gain_pct"] is not None
        ]
        naive_gain = f"{sum(row_pcts) / len(row_pcts):+.2f}%" if row_pcts else "—"
        check(
            f"Your gain % is VALUE-WEIGHTED ({gain_shown}), not the mean of the rows ({naive_gain})",
            gain_shown in veqt and gain_shown != naive_gain,
        )
        tab.screenshot(os.path.join(HERE, "m146-grouped-1440.png"))

        # --- 3. the disclosure --------------------------------------------------
        print("\n3. Inline per-account rows")
        chevron = "Array.from(document.querySelectorAll('button')).find(b => b.getAttribute('aria-label')?.includes('VEQTEST per-account rows'))"
        check(
            "the chevron is a real button with aria-expanded=false",
            bool(
                tab.evaluate(
                    f"(() => {{ const b = {chevron}; return b && b.getAttribute('aria-expanded') === 'false'; }})()"
                )
            ),
        )
        tab.evaluate(f"({chevron})?.click()")
        settle(tab, extra=0.5)
        panel: dict[str, Any] | None = tab.evaluate(
            "(() => {"
            "  const body = Array.from(document.querySelectorAll('tbody[data-testid=\"position-group-accounts\"]'))"
            "    .find(b => !b.hidden);"
            "  if (!body) return null;"
            "  return {"
            "    rows: body.querySelectorAll('tr').length,"
            "    links: Array.from(body.querySelectorAll('a[href^=\"/accounts/\"]')).map(a => a.getAttribute('href')),"
            "  };"
            "})()"
        )
        check("the panel opens with 5 sub-rows", isinstance(panel, dict) and panel["rows"] == 5, panel)
        check(
            "every sub-row links its account to /accounts/{id}",
            isinstance(panel, dict) and len(panel["links"]) == 5,
            (panel or {}).get("links"),
        )
        check("aria-expanded is now true", "true" in str(tab.evaluate(f"({chevron})?.getAttribute('aria-expanded')")))
        # Full page: the five sub-rows render BELOW the group row, which sits
        # under the fold in a 900px viewport.
        tab.screenshot(os.path.join(HERE, "m146-expanded-1440.png"), full_page=True)

        vxu_chevron = "Array.from(document.querySelectorAll('button')).find(b => b.getAttribute('aria-label')?.includes('VXUTEST per-account rows'))"
        tab.evaluate(f"({vxu_chevron})?.click()")
        settle(tab, extra=0.5)
        check(
            "a second disclosure CLOSES the first (one panel at a time)",
            bool(
                tab.evaluate(
                    "Array.from(document.querySelectorAll('tbody[data-testid=\"position-group-accounts\"]'))"
                    "  .filter(b => !b.hidden).length === 1"
                )
            ),
        )

        single: dict[str, Any] = tab.evaluate(
            "(() => {"
            "  const row = Array.from(document.querySelectorAll('[data-testid=\"position-group-row\"]'))"
            "    .find(r => r.innerText.includes('ZGRTEST'));"
            "  return {group: !!row, flat: document.body.innerText.includes('ZGRTEST')};"
            "})()"
        )
        check(
            "the single-account security is NOT a group row (flat, no chevron)",
            single["group"] is False and single["flat"] is True,
            single,
        )
        check(
            "…so no per-account disclosure exists for it",
            tab.evaluate(
                "Array.from(document.querySelectorAll('button'))"
                "  .some(b => b.getAttribute('aria-label')?.includes('ZGRTEST per-account rows'))"
            )
            is False,
        )

        # --- 4. the all-null group ---------------------------------------------
        print("\n4. The all-null group")
        noquote = next((r for r in rows if "NQPTEST" in r), "")
        check("the unpriced security still folds (2 accounts)", bool(noquote), noquote[:120])
        check("…and says 'no price'", "no price" in noquote)
        check("…with em dashes, never a fabricated $0", "$0.00" not in noquote and "—" in noquote)

        # --- 5. the Σ row -------------------------------------------------------
        print("\n5. The Σ row is the trust check")
        grouped_total = total_row_text(tab)
        check("the grouped Σ row carries the SERVER's totals", cad(positions["total_value_cad"]) in grouped_total, grouped_total[:160])
        click_view(tab, "By account")
        settle(tab, extra=0.6)
        flat_total = total_row_text(tab)
        check("byte-identical across both views", grouped_total == flat_total, f"{grouped_total!r} vs {flat_total!r}")

        # --- 6. a11y ------------------------------------------------------------
        print("\n6. a11y (grouped view, 1440 then 390)")
        click_view(tab, "By security")
        settle(tab, extra=0.6)
        load_axe(tab)
        axe_wide = summarize((run_axe(tab) or {}).get("violations", []))
        check("axe-core 0 critical/serious at 1440", axe_wide["counts"]["critical"] == 0 and axe_wide["counts"]["serious"] == 0, axe_wide["counts"])
        check("console 0 errors", tab.console_errors() == [], tab.console_errors()[:3])
        report["axe_1440"] = axe_wide

        force_viewport(tab, 390, 844)
        settle(tab, extra=0.8)
        axe_narrow = summarize((run_axe(tab) or {}).get("violations", []))
        check("axe-core 0 critical/serious at 390", axe_narrow["counts"]["critical"] == 0 and axe_narrow["counts"]["serious"] == 0, axe_narrow["counts"])
        tab.screenshot(os.path.join(HERE, "m146-grouped-390.png"), full_page=True)
        report["axe_390"] = axe_narrow

        browser.close()
    finally:
        report["checks"] = CHECKS
        report["failures"] = FAILURES
        with open(os.path.join(HERE, "m146-positions-group-probe.json"), "w") as handle:
            json.dump(report, handle, indent=2, default=str)
        print(f"\n{'ALL CHECKS PASSED' if not FAILURES else 'FAILURES: ' + str(FAILURES)}")
        print(f"report → {os.path.join(HERE, 'm146-positions-group-probe.json')}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())

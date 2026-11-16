"""M119 — reproduce Mark's Similar-Past-Entries starvation on a shaped tenant.

Mark's screenshot (img_393be74e5c19.png) shows the popover opened from the
NEWEST "Total Tech Pools And Leis" row — the ONLY categorized row of its
payee family — reading "No similar past entries" while nine identical payees
sit in the same table.

This probe seeds a throwaway tenant shaped like his account:

  * 10 rows with the identical payee, dates 2026-09-25 … 2026-05-16,
  * exactly ONE of them categorized ("pool") — the newest, the row the
    popover is opened from in check A,
  * `--filler` extra rows so `GET /transactions?limit=200` really has to page
    (Mark's account is 3415 rows / 18 pages; the substrate is suspect #1).

Two checks, run in one browser session:

  A  anchor = the CATEGORIZED twin   (Mark's click)
  B  anchor = an UNCATEGORIZED sibling

A and B must disagree in exactly one way: B proves the substrate + resolver +
matcher work end-to-end on this data shape, so whatever A shows is caused by
the popover's own options (`excludeId` + `categorizedOnly`), not by a fetch
that never populated.

Evidence (JSON next to this script): the popover's full state sequence, every
GET /api/v1/transactions the click issued with its status / item count /
`total`, the console log, and the offline re-score of the rows the page
actually received.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from probe_target import random_password, strip_passwords

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CHECKS: list[dict] = []
EVIDENCE: dict = {}

HAS_HISTORY_BTN = (
    "(() => !!document.querySelector('tbody tr button[aria-label=\"Similar past entries\"]'))()"
)

# Mark's own ten rows (account 262) — dates and amounts kept so the shape of
# the repro matches the report rather than an invented fixture.
TOTAL_TECH = [
    ("2026-09-25", "-11.24"),
    ("2026-09-23", "-36.10"),
    ("2026-08-19", "-38.99"),
    ("2026-08-07", "-85.88"),
    ("2026-06-17", "-126.00"),
    ("2026-06-11", "-16.89"),
    ("2026-05-26", "-38.99"),
    ("2026-05-19", "-85.88"),
    ("2026-05-19", "-48.25"),
    ("2026-05-16", "-33.79"),
]
PAYEE = "Total Tech Pools And Leis"


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


# ------------------------------------------------------------------- HTTP half
class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token: str | None = None

    def call(self, method: str, path: str, body: object | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, method=method)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        req.data = data
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except Exception as exc:  # pragma: no cover
            return -1, str(exc).encode()

    def json(self, method: str, path: str, body: object | None = None) -> dict:
        status, payload = self.call(method, path, body)
        try:
            parsed = json.loads(payload or b"{}")
        except Exception:
            parsed = {}
        if status >= 400:
            raise RuntimeError(f"{method} {path} -> {status} {payload[:200]!r}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}


# --------------------------------------------------------------- matcher parity
def normalize_text(value: str) -> str:
    import re

    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip().lower()


def trgm_array(text: str) -> list[str]:
    out: list[str] = []
    for word in normalize_text(text).split(" "):
        if not word:
            continue
        buf = f"  {word} "
        for i in range(len(buf) - 2):
            out.append(buf[i : i + 3])
    return out


def similarity(text: str, query: str) -> float:
    a, b = set(trgm_array(text)), set(trgm_array(query))
    if not a or not b:
        return 0.0
    common = len(a & b)
    return common / (len(a) + len(b) - common)


def offline_match(rows, payee, exclude_id, categorized_only, limit=10):
    """The popover's own options, evaluated on the rows the page received.

    `similarity` alone is a lower bound on `greatest(similarity,
    word_similarity)`, so a hit here is definitely a hit in the browser —
    failing in that direction keeps the evidence honest.
    """
    q = normalize_text(payee)
    if q == "":
        return []
    hits = []
    for row in rows:
        if row.get("id") == exclude_id:
            continue
        if categorized_only and row.get("category_id") is None:
            continue
        score = similarity(row.get("normalized_payee") or "", q)
        if score >= 0.3:
            hits.append({"id": row["id"], "score": round(score, 4),
                         "category_id": row.get("category_id")})
    hits.sort(key=lambda h: (-h["score"], -h["id"]))
    return hits[:limit]


# ----------------------------------------------------------------- browser half
def settle(tab, extra: float = 1.2) -> None:
    try:
        tab.wait_for(
            "(() => document.querySelector('[data-testid=\"route-fallback\"]') === null"
            " && document.querySelector('main') !== null)()",
            timeout=25,
        )
    except TimeoutError:
        pass
    tab.sleep(extra)


def seed_in_page(tab) -> dict:
    """Register + sign in inside the page so the refresh cookie lands on origin."""
    stamp = int(time.time() * 1000)
    email = f"m119-{stamp}@example.com"
    password = random_password("m119")
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


def ui_login(tab, base: str, email: str, password: str) -> None:
    tab.navigate(f"{base}/login")
    settle(tab)
    tab.evaluate("document.querySelector('input[type=\"email\"]').focus()")
    tab.send("Input.insertText", {"text": email})
    tab.evaluate("document.querySelector('input[type=\"password\"]').focus()")
    tab.send("Input.insertText", {"text": password})
    tab.press("Enter")
    tab.wait_for("(() => location.pathname !== '/login')()", timeout=25)


def tx_responses(tab) -> list[dict]:
    """The popover's substrate fetch — and ONLY that.

    `limit=200` is `useSearchRows`' PAGE_SIZE; the ledger's own page query and
    the payee filter both use limit=50, so the marker separates the popover's
    fetches from the page's instead of relying on click-time bookkeeping (the
    CDP socket is only drained inside a send, so an event can land either side
    of a snapshot).
    """
    out = []
    for ev in tab.events("Network.responseReceived"):
        url = ev["params"]["response"]["url"]
        if "limit=200" not in url:
            continue
        out.append({"url": url, "status": ev["params"]["response"]["status"],
                    "requestId": ev["params"]["requestId"]})
    return out


def body_of(tab, request_id: str) -> dict | None:
    try:
        res = tab.send("Network.getResponseBody", {"requestId": request_id})
        return json.loads(res.get("body") or "{}")
    except Exception:
        return None


def read_popover(tab, seconds: float = 12.0) -> dict:
    states: list[str] = []
    settled_at: float | None = None
    final: dict | None = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = tab.evaluate(
            """(() => {
              const d = document.querySelector('[role="dialog"]');
              if (!d) return null;
              return {text: d.innerText.replace(/\\n+/g, ' | ').trim(),
                      items: d.querySelectorAll('ul li').length};
            })()"""
        )
        if state is not None and state["text"] not in states:
            states.append(state["text"])
        if state is not None and "Searching" not in state["text"] and state["text"]:
            final = state
            if settled_at is None:
                settled_at = time.time()
            elif time.time() - settled_at > 2.5:
                break
        else:
            settled_at = None
        time.sleep(0.25)
    return {"states": states, "final": final}


def row_summary(tab, n: int = 12) -> list[dict]:
    return tab.evaluate(
        """(() => [...document.querySelectorAll('tbody tr')].slice(0, %d).map(tr => {
          const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
          const cat = [...tr.querySelectorAll('td')]
            .find(td => td.querySelector('input[placeholder*="Type to search"], select'));
          return {cells, category: cat ? cat.innerText.trim() : null};
        }))()"""
        % n
    ) or []


def open_popover(tab, index: int) -> dict:
    """Click the `index`-th visible row's history button, then read the panel."""
    clicked = tab.evaluate(
        """(() => {
          const rows = [...document.querySelectorAll('tbody tr')];
          const tr = rows[%d];
          if (!tr) return null;
          const btn = tr.querySelector('button[aria-label="Similar past entries"]');
          if (!btn) return null;
          const r = btn.getBoundingClientRect();
          return {payee: (tr.querySelector('td.font-medium') || {}).innerText || null,
                  x: r.x + r.width / 2, y: r.y + r.height / 2};
        })()""" % index
    )
    if not clicked:
        return {"clicked": None}
    tab.click_at(clicked["x"], clicked["y"])
    out = read_popover(tab)
    out["clicked"] = clicked
    out["screenshot_taken"] = False
    # dismiss for the next case: Escape closes every surface dialog
    tab.press("Escape")
    tab.sleep(0.4)
    return out


# ------------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--filler", type=int, default=450)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--out", default=str(HERE / "m119-similar-repro.json"))
    ap.add_argument(
        "--expect",
        choices=["before", "after"],
        default="after",
        help="before = the bug reproduces (case A empty); "
        "after = the fix ships (case A lists the siblings)",
    )
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m119_similar_repro.py")
    base = args.base.rstrip("/")

    import contextlib
    import tempfile

    os.environ.setdefault("A11Y_CDP_PORT", "9468")
    os.environ.setdefault("A11Y_PROFILE_DIR", os.path.join(tempfile.gettempdir(), "m119-repro-profile"))

    from a11y_driver import Browser

    api = Api(base)

    with contextlib.closing(Browser(width=args.width, height=900)) as browser:
        tab = browser.new_tab(base + "/login")
        tab.enable_runtime()
        tab.send("Network.enable")
        settle(tab)

        # --- seed ----------------------------------------------------------
        creds = seed_in_page(tab)
        check("seed tenant", creds["register"] == 200 and creds["login"] == 200, json.dumps(strip_passwords(creds)))
        logged = api.json("POST", "/api/v1/auth/login",
                          {"email": creds["email"], "password": creds["password"]})
        api.token = logged.get("access_token")
        check("bearer token", bool(api.token))

        account = api.json("POST", "/api/v1/accounts",
                           {"name": "Probe Visa", "type": "credit_card", "currency": "CAD"})
        account_id = account["id"]
        category = api.json("POST", "/api/v1/categories", {"name": "pool"})
        category_id = category["id"]

        tt_ids: list[int] = []
        for date, amount in TOTAL_TECH:
            row = api.json("POST", "/api/v1/transactions",
                           {"account_id": account_id, "date": date,
                            "raw_description": PAYEE, "normalized_payee": PAYEE,
                            "amount_native": amount, "currency": "CAD"})
            tt_ids.append(row["id"])
        # the NEWEST twin is the categorized one — Mark's clicked row
        api.json("PATCH", f"/api/v1/transactions/{tt_ids[0]}", {"category_id": category_id})

        made = 0
        for i in range(args.filler):
            day = 1 + (i % 28)
            month = 1 + (i % 12)
            body = {"account_id": account_id,
                    "date": f"2025-{month:02d}-{day:02d}",
                    "raw_description": f"FILLER MERCHANT {i:04d}",
                    "normalized_payee": f"Filler Merchant {i:04d}",
                    "amount_native": f"-{10 + (i % 90)}.{(i % 100):02d}",
                    "currency": "CAD"}
            res = api.call("POST", "/api/v1/transactions", body)
            if res[0] < 400:
                made += 1
        check("seed rows", made >= args.filler,
              f"{len(tt_ids)} identical-payee + {made}/{args.filler} filler rows, "
              f"category {category_id}")

        total_rows = api.json("GET", "/api/v1/transactions?limit=1&offset=0")
        EVIDENCE["tenant_total"] = total_rows.get("total")
        check("substrate pages", (total_rows.get("total") or 0) > 200,
              f"total={total_rows.get('total')} (>200 forces the client's page loop)")

        # --- drive the UI --------------------------------------------------
        ui_login(tab, base, creds["email"], creds["password"])
        tab.navigate(base + "/transactions")
        settle(tab)
        tab.wait_for(HAS_HISTORY_BTN, timeout=30)

        # payee filter so the visible table is Mark's 10 identical rows
        tab.evaluate(
            """(() => {
              const label = [...document.querySelectorAll('label')]
                .find(l => l.textContent.trim().startsWith('Payee contains'));
              const input = label.querySelector('input');
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
              setter.call(input, %s);
              input.dispatchEvent(new Event('input', {bubbles: true}));
              input.dispatchEvent(new Event('change', {bubbles: true}));
              return input.value;
            })()""" % json.dumps("Total Tech")
        )
        tab.sleep(0.3)
        tab.evaluate(
            """(() => { const b = [...document.querySelectorAll('button')]
              .find(el => el.textContent.trim() === 'Apply filters'); b.click();
              return true; })()"""
        )
        tab.wait_for(HAS_HISTORY_BTN, timeout=30)
        tab.sleep(1.0)

        EVIDENCE["visible_rows"] = row_summary(tab, 12)

        # A — anchor = the CATEGORIZED twin (Mark's click)
        case_a = open_popover(tab, 0)
        case_a["network"] = [
            {"requestId": r["requestId"],
             "path": urllib.parse.urlparse(r["url"]).path + "?"
             + urllib.parse.urlparse(r["url"]).query,
             "status": r["status"],
             "items": len((body_of(tab, r["requestId"]) or {}).get("items") or []),
             "total": (body_of(tab, r["requestId"]) or {}).get("total")}
            for r in tx_responses(tab)
        ]
        EVIDENCE["case_a"] = case_a

        # B — anchor = an UNCATEGORIZED sibling (reuses the cached substrate)
        case_b = open_popover(tab, 1)
        case_b["network"] = [
            urllib.parse.urlparse(r["url"]).query for r in tx_responses(tab)
        ]
        EVIDENCE["case_b"] = case_b

        # offline parity on the rows the page really received
        rows: list[dict] = []
        seen: set[int] = set()
        # case A's own substrate requests (requestId survives in the record);
        # Network.getResponseBody is re-readable, so this second pass is safe.
        for r in case_a.get("network", []):
            body = body_of(tab, r["requestId"]) or {}
            for it in body.get("items") or []:
                if it["id"] not in seen:
                    seen.add(it["id"])
                    rows.append(it)
        EVIDENCE["substrate_rows_seen"] = len(rows)
        EVIDENCE["offline_case_a"] = offline_match(
            rows, PAYEE, exclude_id=tt_ids[0], categorized_only=True)
        EVIDENCE["offline_case_a_uncategorized_ok"] = offline_match(
            rows, PAYEE, exclude_id=tt_ids[0], categorized_only=False)
        EVIDENCE["offline_case_b"] = offline_match(
            rows, PAYEE, exclude_id=tt_ids[1], categorized_only=True)
        EVIDENCE["console"] = tab.console_errors()[:20]

        shot = args.out.replace(".json", ".png")
        tab.screenshot(shot)
        EVIDENCE["screenshot"] = shot

        # --- verdicts ------------------------------------------------------
        a_final = (case_a.get("final") or {}).get("text", "")
        b_final = (case_b.get("final") or {}).get("text", "")
        check("substrate populated",
              EVIDENCE["substrate_rows_seen"] > 200,
              f"{EVIDENCE['substrate_rows_seen']} rows arrived at the page; "
              f"requests {[(p['path'], p['status'], p['items'], p['total']) for p in case_a['network']]}")
        check("console clean", not EVIDENCE["console"],
              "; ".join(EVIDENCE["console"])[:300] or "no errors/warnings/CSP refusals")
        check("B (uncategorized anchor) shows the categorized twin",
              (case_b.get("final") or {}).get("items", 0) > 0,
              f"{b_final[:160]!r}")
        a_items = (case_a.get("final") or {}).get("items", 0)
        b_items = (case_b.get("final") or {}).get("items", 0)
        if args.expect == "before":
            check("A (categorized anchor) reproduces the report",
                  a_items == 0 and "No similar past entries" in a_final,
                  f"{a_final[:160]!r}")
        else:
            check("A (categorized anchor) lists its 9 siblings",
                  a_items == 9 and "No similar past entries" not in a_final,
                  f"items={a_items} {a_final[:160]!r}")
            # 6919 is the family's ONLY categorized row and its date is
            # 2026-09-25, so "the first listed row is 2026-09-25" IS the
            # categorized-first claim, read straight off the rendered panel.
            check("B (uncategorized anchor) lists 9 with the `pool` row first",
                  b_items == 9
                  and b_final.startswith(
                      "SIMILAR PAST ENTRIES | Total Tech Pools And Leis | 2026-09-25"
                  ),
                  f"items={b_items} {b_final[:200]!r}")

        print(json.dumps({k: v for k, v in EVIDENCE.items() if k != "visible_rows"}, indent=2)[:3000])

    Path(args.out).write_text(json.dumps({"checks": CHECKS, "evidence": EVIDENCE}, indent=2))
    print(f"\nevidence -> {args.out}")
    return 0 if all(c["ok"] for c in CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

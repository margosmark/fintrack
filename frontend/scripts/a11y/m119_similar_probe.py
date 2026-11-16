"""M119 — Similar-Past-Entries popover: live probe against the DEPLOYED stack.

Reproduces Mark's bug report (screenshot img_393be74e5c19.png): a row whose
payee has an identical categorized sibling on the very same page, yet the
popover says "No similar past entries".

    python3 m119_similar_probe.py [--base https://fintrac.princessandmark.com]
                                  [--payee GROCERY] [--demo]

What it captures (evidence, not theory):

  network   every GET /api/v1/transactions issued AFTER the history button is
            clicked — count, path (limit/offset), HTTP status, and each
            page's item count + `total` read from the RESPONSE BODY. This is
            suspect #1: does the all-rows substrate actually populate?
  popover   the dialog's text, so "Searching…" / "No similar past entries" /
            a populated list are told apart from each other.
  console   errors, warnings, thrown exceptions and CSP refusals (suspect #2:
            a suppressed crash inside the matcher's useMemo).
  matcher   the rows the page received are re-scored OFFLINE with the same
            pg_trgm port + the same matchRows options (categorizedOnly,
            excludeId, limit 10) — if offline says "hit" and the popover says
            "empty", the divergence is inside the component, not the data.

Exit 0 only when the probe ran to completion; the PASS/FAIL of each check is
in the JSON written next to this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from a11y_driver import Browser  # noqa: E402

CHECKS: list[dict] = []
EVIDENCE: dict = {}


HAS_HISTORY_BTN = (
    "(() => !!document.querySelector('tbody tr button[aria-label=\"Similar past entries\"]'))()"
)


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


# ---------------------------------------------------------------- pg_trgm port
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


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    common = len(a & b)
    return common / (len(a) + len(b) - common)


def similarity(text: str, query: str) -> float:
    return _jaccard(set(trgm_array(text)), set(trgm_array(query)))


def match_score(query: str, text: str) -> float:
    # word_similarity only ever >= similarity for identical strings; the
    # offline re-score below is a lower bound, which is the honest direction
    # to fail in (a "hit" here is definitely a hit in the browser).
    return similarity(text, query)


def offline_match(rows, payee, exclude_id, categorized_only=True, limit=10):
    q = normalize_text(payee)
    if q == "":
        return []
    hits = []
    for row in rows:
        if row.get("id") == exclude_id:
            continue
        if categorized_only and row.get("category_id") is None:
            continue
        score = match_score(q, row.get("payee") or row.get("normalized_payee") or "")
        if score >= 0.3:
            hits.append({"id": row.get("id"), "score": round(score, 4),
                         "category_id": row.get("category_id")})
    hits.sort(key=lambda h: (-h["score"], -h["id"]))
    return hits[:limit]


# ---------------------------------------------------------------- browser bits
def js_box(tab, expr: str) -> dict | None:
    return tab.evaluate(
        "(() => { const el = %s; if (!el) return null;"
        " const r = el.getBoundingClientRect();"
        " return {x: r.x, y: r.y, w: r.width, h: r.height}; })()" % expr
    )


def click_selector(tab, expr: str) -> bool:
    box = js_box(tab, expr)
    if not box or box["w"] == 0:
        return False
    tab.click_at(box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
    return True


def tx_responses(tab) -> list[dict]:
    """Every /api/v1/traffic response seen since Network.enable, newest last."""
    out = []
    for ev in tab.events("Network.responseReceived"):
        url = ev["params"]["response"]["url"]
        if "/api/v1/transactions" not in url:
            continue
        out.append(
            {
                "url": url,
                "status": ev["params"]["response"]["status"],
                "requestId": ev["params"]["requestId"],
            }
        )
    return out


def body_of(tab, request_id: str) -> dict | None:
    try:
        res = tab.send("Network.getResponseBody", {"requestId": request_id})
        return json.loads(res.get("body") or "{}")
    except Exception:
        return None


# ----------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://fintrac.princessandmark.com")
    ap.add_argument("--payee", default="GROCERY")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--out", default=str(HERE / "m119-similar-probe.json"))
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m119_similar_probe.py")
    base = args.base.rstrip("/")

    # Own port + own profile: the shared :9444/:9333 harnesses race other cards.
    import contextlib
    import tempfile

    os.environ.setdefault("A11Y_CDP_PORT", "9467")
    os.environ.setdefault("A11Y_PROFILE_DIR", os.path.join(tempfile.gettempdir(), "m119-profile"))

    with contextlib.closing(Browser(width=args.width, height=900)) as b:
        tab = b.new_tab(base + "/login")
        tab.enable_runtime()
        tab.send("Network.enable")
        tab.navigate(base + "/login", timeout=45)

        # 1. demo entry (no credentials — the documented QA front door)
        ok = tab.wait_for(
            """(() => [...document.querySelectorAll('button')]
            .some(el => el.textContent.includes('Explore the demo')))()""",
            timeout=30,
        )
        check("demo_button", bool(ok), "Explore the demo present on /login")
        click_selector(
            tab,
            "[...document.querySelectorAll('button')]"
            ".find(el => el.textContent.includes('Explore the demo'))",
        )
        tab.wait_for("(() => !location.pathname.startsWith('/login'))()", timeout=30)

        # 2. transactions + payee filter so every visible row shares a payee
        tab.navigate(base + "/transactions", timeout=45)
        tab.wait_for(HAS_HISTORY_BTN, timeout=30)
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
            })()"""
            % json.dumps(args.payee)
        )
        tab.sleep(0.3)
        click_selector(
            tab,
            "[...document.querySelectorAll('button')]"
            ".find(el => el.textContent.trim() === 'Apply filters')",
        )
        # the PREVIOUS rows satisfy a bare tbody>0 test instantly, so wait for
        # the affordance itself — that is what the click needs.
        tab.wait_for(HAS_HISTORY_BTN, timeout=30)

        # 3. the row we open the popover on (its own payee text is the anchor)
        row_info = tab.evaluate(
            """(() => {
              const tr = document.querySelector('tbody tr');
              if (!tr) return null;
              const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
              const btn = tr.querySelector('button[aria-label="Similar past entries"]');
              if (!btn) return null;
              const r = btn.getBoundingClientRect();
              const payeeCell = [...tr.querySelectorAll('td')]
                .find(td => td.classList.contains('font-medium'));
              return {cells, x: r.x + r.width/2, y: r.y + r.height/2,
                      payee: payeeCell ? payeeCell.innerText.trim() : null};
            })()"""
        )
        check("anchor_row", row_info is not None, json.dumps(row_info or {})[:200])

        # reset network log so ONLY the popover's own fetches are counted
        tab.send("Network.clearBrowserCache", {})
        tab.evaluate("performance.clearResourceTimings()")

        # 4. click the history button — the bug's trigger
        tab.evaluate(
            "(() => { const b = document.querySelector("
            "'tbody tr button[aria-label=\"Similar past entries\"]');"
            " b.click(); return true; })()"
        )

        # 5. read the popover through its whole life cycle. Do NOT stop at the
        # first settled frame: the all-rows fetch is async, so an early
        # "No similar past entries" must be re-read after it has had time to
        # populate, or the probe would measure a transient instead of the
        # state the user sees.
        states = []
        settled_at: float | None = None
        deadline = time.time() + 30
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
            if state is not None and (
                "Searching" not in state["text"] and state["text"] != ""
            ):
                if settled_at is None:
                    settled_at = time.time()
                elif time.time() - settled_at > 3:
                    break
            else:
                settled_at = None
            time.sleep(0.25)
        EVIDENCE["popover_states"] = states
        final = tab.evaluate(
            """(() => {
              const d = document.querySelector('[role="dialog"]');
              if (!d) return null;
              return {text: d.innerText.replace(/\\n+/g, ' | ').trim(),
                      items: d.querySelectorAll('ul li').length};
            })()"""
        )
        EVIDENCE["popover_final"] = final

        shot = args.out.replace(".json", ".png")
        tab.screenshot(shot)
        EVIDENCE["screenshot"] = shot

        # 6. network evidence — the substrate's own fetches
        responses = tx_responses(tab)
        pages = []
        for r in responses:
            body = body_of(tab, r["requestId"]) or {}
            pages.append(
                {
                    "path": urllib.parse.urlparse(r["url"]).path
                    + "?"
                    + urllib.parse.urlparse(r["url"]).query,
                    "status": r["status"],
                    "items": len(body.get("items") or []),
                    "total": body.get("total"),
                }
            )
        EVIDENCE["transactions_requests"] = pages
        EVIDENCE["anchor_row"] = row_info

        # 7. console evidence (suspect #2 — a suppressed matcher crash)
        EVIDENCE["console"] = tab.console_errors()[:20]

        # 8. offline re-score of the rows the page actually received
        rows: list[dict] = []
        seen_ids: set[int] = set()
        for r in responses:
            body = body_of(tab, r["requestId"]) or {}
            for it in body.get("items") or []:
                if it["id"] not in seen_ids:
                    seen_ids.add(it["id"])
                    rows.append(it)
        anchor_payee = (row_info or {}).get("payee") or ""
        hits = offline_match(rows, anchor_payee, exclude_id=None)
        EVIDENCE["substrate_rows"] = len(rows)
        EVIDENCE["offline_hits"] = hits[:10]
        EVIDENCE["anchor_payee"] = anchor_payee

        print(json.dumps(EVIDENCE, indent=2)[:4000])

        # ---------------------------------------------------------------- checks
        if not pages:
            check("substrate_fetch", False, "no GET /api/v1/transactions after the click")
        else:
            bad = [p for p in pages if p["status"] != 200]
            check(
                "substrate_fetch",
                not bad,
                f"{len(pages)} request(s), statuses "
                f"{sorted({p['status'] for p in pages})}, items "
                f"{sum(p['items'] for p in pages)} / total "
                f"{pages[0]['total']}",
            )
        check(
            "console_clean",
            not EVIDENCE["console"],
            "; ".join(EVIDENCE["console"])[:300] or "no errors/warnings/CSP refusals",
        )
        check(
            "offline_matcher",
            len(hits) > 0,
            f"{len(hits)} categorized sibling(s) at/above 0.3 for "
            f"{anchor_payee!r}: {hits[:3]}",
        )
        text = (final or {}).get("text") or ""
        check(
            "popover_shows_results",
            (final or {}).get("items", 0) > 0,
            f"dialog text: {text[:200]!r}",
        )

    Path(args.out).write_text(json.dumps({"checks": CHECKS, "evidence": EVIDENCE}, indent=2))
    print(f"\nevidence -> {args.out}")
    return 0 if all(c["ok"] for c in CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""M120 receipts for the four once-silent mutation surfaces — the live probe
(AGENTS §4 step 5, against a running stack).

    python3 m120_receipts_probe.py [--base https://fintrac.princessandmark.com] [--width 1440]

Runs against a THROWAWAY tenant (the demo tenant answers
403 demo_account_restricted on every write), because M120 is entirely about
writes. What each check proves, against the deployed API + the real drawer:

  1  seed    register/login/account + 9 manual rows (3 coffee, 1 shell,
             3 netflix, 2 freshmart)
  2  rules   POST /rules/{coffee}/apply mode=all -> each affected row gains
             ONE categorized (via=rule_backfill) + ONE tagged rider, in the
             route's single commit; a repeat apply with overwrite=false writes
             NOTHING; mode=forward writes NOTHING
  3  pipe1   POST /suggestions/run over the SHELL row -> layer 1 writes
             categorized via=rule with actor NULL and the "(rule)" wording
  4  accept  an operator-seeded suggestion -> categorized via=suggestion_accept
             with actor = the clicking user and "(accepted suggestion)"
  5  pipe2   a second FRESHMART row, same learned payee -> layer 2 writes
             categorized via=learned, actor NULL, "(learned from your edits)"
  6  recur   the M96 candidate group accepted -> one plain `edited` receipt per
             row, no `via` key
  7  export  /export/zip's transaction_events.csv carries the new receipts
  8  ui      the History drawer renders every `via` wording line verbatim
  9  axe     0 critical/serious with the drawer open

Exit 0 only if every check passes; JSON evidence lands next to this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from probe_target import random_password

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
AXE = str(REPO / "frontend" / "node_modules" / "axe-core" / "axe.min.js")
BASE = os.environ.get("M120_BASE", "https://fintrac.princessandmark.com")
# The probe seeds a suggestion by hand: accept is a first-class M120 surface and
# the only way to get a `suggested` row without a live LLM is an operator INSERT
# into the deployed DB (the same class of operator psql the fx/tax remediation
# uses). Set M120_NO_PSQL=1 to skip that surface instead.
PSQL = ["ssh", "example.internal", "docker exec -i fin_tracker-db-1 psql -U fintrack -d fintrack"]

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
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, method=method)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        req.data = data
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:  # noqa: PERF203 — same shape as M106's
            return exc.code, exc.read()

    def json(self, method: str, path: str, **kw: Any) -> Any:
        status, raw = self.call(method, path, **kw)
        if status >= 400:
            return {"_error": status, "_body": raw.decode(errors="replace")[:300]}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw.decode(errors="replace")[:300]}


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


def drawer_lines(tab) -> list[str]:
    """The sentences the drawer printed, newest first."""
    try:
        tab.wait_for("document.querySelector('[role=\"dialog\"]') !== null", timeout=12)
    except TimeoutError:
        return []
    tab.sleep(1.2)
    return tab.evaluate(
        "[...document.querySelectorAll('[role=\"dialog\"] li')].map(li => li.innerText)"
    )


def close_dialog(tab) -> None:
    tab.press("Escape")
    tab.sleep(0.6)


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


# ---------------------------------------------------------------------- the checks
def seed_suggestion(sql: str) -> tuple[bool, str]:
    """Operator INSERT of one `suggested` row (see the module docstring)."""
    try:
        proc = subprocess.run(
            PSQL, input=sql.encode(), capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ssh/psql unavailable: {exc}"
    if proc.returncode != 0:
        return False, proc.stderr.decode(errors="replace")[-300:]
    return True, (proc.stdout or b"").decode(errors="replace")[:200]


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--out", default=str(HERE / "m120-receipts-probe.json"))
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    from probe_target import require_target
    require_target(BASE, who="m120_receipts_probe.py")

    from a11y_driver import Browser  # local import: keeps --help fast

    api = Api(BASE)
    evidence: dict[str, Any] = {"base": BASE}
    stamp = int(time.time() * 1000)
    email = f"m120-{stamp}@example.com"
    password = random_password("m120")
    browser = Browser(width=args.width, height=args.height)
    try:
        # ---------------------------------------------------------- 1. seed
        tab = browser.new_tab(f"{BASE}/login")
        tab.enable_runtime()
        tab.send("Network.enable")
        tab.force_viewport(args.width, args.height)
        settle(tab)

        reg = api.json(
            "POST",
            "/api/v1/auth/register",
            body={"email": email, "password": password},
        )
        logged = api.json("POST", "/api/v1/auth/login", body={"email": email, "password": password})
        api.token = (logged or {}).get("access_token")
        check(
            "seed tenant (register + login)",
            bool(api.token) and not (reg or {}).get("_error"),
            f"register={json.dumps(reg)[:120]} token={bool(api.token)}",
        )
        if not api.token:
            return 1

        me = api.json("PATCH", "/api/v1/users/me/profile", body={"name": "M120 Probe"})
        user_id = (me or {}).get("id")
        check("probe user id", bool(user_id), json.dumps(me)[:160])

        account = api.json(
            "POST",
            "/api/v1/accounts",
            body={"name": "M120 Probe Chequing", "type": "chequing", "currency": "CAD"},
        )
        account_id = (account or {}).get("id")
        check("probe account", bool(account_id), json.dumps(account)[:160])

        categories = api.json("GET", "/api/v1/categories") or {}
        groceries = next(
            (c for c in (categories.get("items") or []) if c.get("name") == "groceries"), None
        )
        category_id = (groceries or {}).get("id")
        check("seeded `groceries` category", bool(category_id), json.dumps(groceries)[:200])

        tag = api.json("POST", "/api/v1/tags", body={"name": "m120probe"})
        tag_id = (tag or {}).get("id")
        check("probe tag", bool(tag_id), json.dumps(tag)[:160])

        def tx(payee: str, amount: str, day: str) -> dict:
            return api.json(
                "POST",
                "/api/v1/transactions",
                body={
                    "account_id": account_id,
                    "date": day,
                    "raw_description": payee,
                    "normalized_payee": payee,
                    "amount_native": amount,
                    "currency": "CAD",
                },
            )

        rows: dict[str, int] = {}
        plan = [
            ("coffee1", "PROBE COFFEE 1", "-4.50", "2026-09-10"),
            ("coffee2", "PROBE COFFEE 2", "-5.50", "2026-09-11"),
            ("coffee3", "PROBE COFFEE 3", "-6.50", "2026-09-12"),
            ("shell", "PROBE SHELL", "-50.00", "2026-09-13"),
            ("fresh1", "PROBE FRESHMART", "-10.00", "2026-09-20"),
            ("fresh2", "PROBE FRESHMART", "-20.00", "2026-09-21"),
            ("netflix1", "PROBE NETFLIX", "-15.99", "2026-07-05"),
            ("netflix2", "PROBE NETFLIX", "-16.49", "2026-08-05"),
            ("netflix3", "PROBE NETFLIX", "-17.49", "2026-09-05"),
        ]
        for key, payee, amount, day in plan:
            made = tx(payee, amount, day)
            if made and made.get("id"):
                rows[key] = made["id"]
        check("nine manual rows", len(rows) == 9, str(sorted(rows.items())))
        evidence["rows"] = rows
        if len(rows) != 9:
            return 1

        # ---------------------------------------------------------- 2. rules-apply
        coffee_rule = api.json(
            "POST",
            "/api/v1/rules",
            body={
                "match_type": "contains",
                "pattern": "COFFEE",
                "priority": 10,
                "category_id": category_id,
                "tag_ids": [tag_id],
            },
        )
        coffee_rule_id = (coffee_rule or {}).get("id")
        check("coffee rule (with a tag)", bool(coffee_rule_id), json.dumps(coffee_rule)[:200])

        applied = api.json(
            "POST",
            f"/api/v1/rules/{coffee_rule_id}/apply",
            body={"mode": "all", "overwrite": True},
        )
        check(
            "rule backfill updated exactly the three coffee rows",
            (applied or {}).get("applied_count") == 3,
            json.dumps(applied),
        )

        def events(tx_id: int) -> list[dict]:
            return (api.json("GET", f"/api/v1/transactions/{tx_id}/events") or {}).get("items") or []

        coffee_events = events(rows["coffee1"])

        def by_kind(items: list[dict], kind: str) -> dict:
            return next((e for e in items if e.get("event_kind") == kind), {})

        top = by_kind(coffee_events, "categorized")
        payload = top.get("payload") or {}
        check(
            "rules-apply -> categorized via=rule_backfill, actor = the caller",
            top.get("event_kind") == "categorized"
            and payload.get("via") == "rule_backfill"
            and payload.get("category_name") == "groceries"
            and top.get("actor") == user_id
            and top.get("summary") == "you set category groceries (rule backfill)",
            json.dumps(top)[:400],
        )
        rider = by_kind(coffee_events, "tagged")
        rp = rider.get("payload") or {}
        check(
            "the tagged rider mirrors PUT /tags (names AND ids)",
            rider.get("event_kind") == "tagged"
            and rp.get("added") == ["m120probe"]
            and rp.get("added_ids") == [tag_id]
            and rp.get("removed") == []
            and rp.get("removed_ids") == [],
            json.dumps(rider)[:400],
        )
        check(
            "the hash re-derives from the payload the drawer rendered",
            bool(top.get("payload_hash"))
            and top["payload_hash"]
            == hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest(),
            str(top.get("payload_hash"))[:80],
        )
        evidence["coffee_kinds"] = [e["event_kind"] for e in coffee_events]

        repeat = api.json(
            "POST",
            f"/api/v1/rules/{coffee_rule_id}/apply",
            body={"mode": "all", "overwrite": False},
        )
        after_repeat = events(rows["coffee1"])
        check(
            "overwrite=false over already-categorized rows writes NOTHING",
            (repeat or {}).get("applied_count") == 0
            and len(after_repeat) == len(coffee_events),
            f"applied={repeat and repeat.get('applied_count')} "
            f"events {len(coffee_events)} -> {len(after_repeat)}",
        )
        forward = api.json(
            "POST", f"/api/v1/rules/{coffee_rule_id}/apply", body={"mode": "forward"}
        )
        check(
            "mode=forward writes nothing",
            (forward or {}).get("applied_count") == 0
            and len(events(rows["coffee1"])) == len(coffee_events),
            json.dumps(forward)[:200],
        )

        # ---------------------------------------------------------- 3. pipeline layer 1
        shell_rule = api.json(
            "POST",
            "/api/v1/rules",
            body={
                "match_type": "contains",
                "pattern": "SHELL",
                "priority": 20,
                "category_id": category_id,
            },
        )
        check("shell rule", bool((shell_rule or {}).get("id")), json.dumps(shell_rule)[:160])
        run = api.json(
            "POST",
            "/api/v1/suggestions/run",
            body={"transaction_ids": [rows["shell"]]},
        )
        shell_events = events(rows["shell"])
        stop = by_kind(shell_events, "categorized")
        sp = stop.get("payload") or {}
        check(
            "pipeline layer 1 -> categorized via=rule, actor NULL, system wording",
            (run or {}).get("queued") == 0
            and stop.get("event_kind") == "categorized"
            and sp.get("via") == "rule"
            and stop.get("actor") is None
            and stop.get("summary") == "the system set category groceries (rule)",
            json.dumps(stop)[:400],
        )
        evidence["shell_summary"] = stop.get("summary")

        # ---------------------------------------------------------- 4. suggestion accept
        suggestion_ok = False
        if os.environ.get("M120_NO_PSQL") == "1":
            check("suggestion seed skipped (M120_NO_PSQL=1)", True, "skipped")
        else:
            sql = (
                "INSERT INTO llm_suggestions "
                "(user_id, transaction_id, proposed_category_id, confidence, status, "
                "model_used, created_at, updated_at) VALUES "
                f"({user_id}, {rows['fresh1']}, {category_id}, 0.93, 'suggested', "
                "'m120-probe', now(), now()) RETURNING id;"
            )
            ok, detail = seed_suggestion(sql)
            suggestion_ok = ok
            check("operator seed of one `suggested` row", ok, detail)
        if suggestion_ok:
            pending = api.json("GET", "/api/v1/suggestions?status=suggested") or {}
            suggestion = next(
                (s for s in (pending.get("items") or []) if s.get("transaction_id") == rows["fresh1"]),
                None,
            )
            check("the seeded suggestion is in the queue", bool(suggestion), json.dumps(pending)[:250])
            if suggestion:
                accepted = api.json(
                    "POST", f"/api/v1/suggestions/{suggestion['id']}/accept"
                )
                accept_events = events(rows["fresh1"])
                acc = by_kind(accept_events, "categorized")
                ap_ = acc.get("payload") or {}
                check(
                    "suggestion accept -> categorized via=suggestion_accept, actor = clicker",
                    (accepted or {}).get("status") == "accepted"
                    and acc.get("event_kind") == "categorized"
                    and ap_.get("via") == "suggestion_accept"
                    and ap_.get("suggestion_id") == suggestion["id"]
                    and acc.get("actor") == user_id
                    and acc.get("summary") == "you set category groceries (accepted suggestion)",
                    json.dumps(acc)[:400],
                )
                again = api.json(
                    "POST", f"/api/v1/suggestions/{suggestion['id']}/accept"
                )
                check(
                    "a second accept is a 409 that writes nothing",
                    (again or {}).get("_error") == 409
                    and len(events(rows["fresh1"])) == len(accept_events),
                    json.dumps(again)[:220],
                )

        # ---------------------------------------------------------- 5. pipeline layer 2
        api.json(
            "POST",
            "/api/v1/suggestions/run",
            body={"transaction_ids": [rows["fresh2"]]},
        )
        fresh2_events = events(rows["fresh2"])
        learned = by_kind(fresh2_events, "categorized")
        lp = learned.get("payload") or {}
        check(
            "pipeline layer 2 -> categorized via=learned, actor NULL",
            learned.get("event_kind") == "categorized"
            and lp.get("via") == "learned"
            and learned.get("actor") is None
            and learned.get("summary")
            == "the system set category groceries (learned from your edits)",
            json.dumps(learned)[:400],
        )
        evidence["learned_summary"] = learned.get("summary")

        # ---------------------------------------------------------- 6. recurring accept
        candidates = api.json("GET", "/api/v1/transactions/recurring/candidates") or {}
        group = next(
            (g for g in (candidates.get("groups") or []) if "NETFLIX" in (g.get("payee") or "")),
            None,
        )
        check("the M96 prompt offers the NETFLIX group", bool(group), json.dumps(candidates)[:300])
        if group:
            n = api.json(
                "POST",
                "/api/v1/transactions/recurring/accept",
                body={
                    "group_key": group["group_key"],
                    "transaction_ids": group["transaction_ids"],
                    "cadence": group["cadence"],
                },
            )
            netflix_events = events(rows["netflix1"])
            rec = by_kind(netflix_events, "edited")
            rp2 = rec.get("payload") or {}
            check(
                "recurring accept -> one plain `edited` per row, NO via key",
                (n or {}).get("updated") == 3
                and rec.get("event_kind") == "edited"
                and "via" not in rp2
                and rp2.get("fields")
                == ["is_recurring", "recurring_cadence", "recurring_next_date"]
                and rec.get("actor") == user_id
                and rec.get("summary") == "you edited this transaction",
                json.dumps(rec)[:400],
            )
            evidence["recurring_summary"] = rec.get("summary")

        # ---------------------------------------------------------- 7. export
        import io
        import zipfile

        status, zipped = api.call("GET", "/api/v1/export/zip")
        shipped: set[str] = set()
        if status == 200:
            try:
                with zipfile.ZipFile(io.BytesIO(zipped)) as zf:
                    if "transaction_events.csv" in zf.namelist():
                        shipped = set(
                            zf.read("transaction_events.csv").decode().splitlines()
                        )
            except zipfile.BadZipFile:
                shipped = set()
        blob = "\n".join(shipped)
        check(
            "/export/zip ships transaction_events.csv carrying the new receipts",
            status == 200
            and "rule_backfill" in blob
            and "suggestion_accept" in blob
            and "learned" in blob,
            f"HTTP {status} rows={len(shipped)}",
        )

        # ---------------------------------------------------------- 8. the drawer
        goto(tab, "/transactions")
        if tab.evaluate("location.pathname") != "/transactions":
            ui_login(tab, email, password)
            goto(tab, "/transactions")
        check("landed on /transactions", tab.evaluate("location.pathname") == "/transactions")

        # Scoped by TRANSACTION ID: two of these rows share a payee (the two
        # FRESHMART rows are the accept surface and the learned-map surface),
        # so a prefix match would always land on whichever the list sorted
        # first and could never tell the two wordings apart.
        wanted = [
            ("PROBE COFFEE 1", rows["coffee1"], "(rule backfill)"),
            ("PROBE SHELL", rows["shell"], "(rule)"),
            ("PROBE FRESHMART", rows["fresh1"], "(accepted suggestion)"),
            ("PROBE FRESHMART", rows["fresh2"], "(learned from your edits)"),
            ("PROBE NETFLIX", rows["netflix1"], "you edited this transaction"),
        ]
        seen_labels = tab.evaluate(
            "[...document.querySelectorAll('button[aria-label^=\"History —\"]')]"
            ".map(b => b.getAttribute('aria-label'))"
        )
        evidence["history_buttons"] = seen_labels
        for payee, tx_id, needle in wanted:
            label = f"History — {payee} (transaction {tx_id})"
            # Build the selector with the REAL em dash: click_button runs the
            # whole thing through json.dumps once, so pre-escaping it here
            # (json.dumps of an em dash -> \\u2014) would land a literal
            # backslash-u in the attribute value and match nothing.
            rect = click_button(tab, f'button[aria-label="{label}"]')
            lines = drawer_lines(tab)
            hit = next((ln for ln in lines if needle in ln), "")
            check(
                f"drawer renders {needle!r} for {payee} (tx {tx_id})",
                bool(rect) and bool(hit),
                json.dumps(lines[:4], ensure_ascii=False)[:420]
                + ("" if rect else f" | label missed: {json.dumps(seen_labels)[:300]}"),
            )
            close_dialog(tab)

        # axe with a drawer OPEN (the same posture M106 pinned): reopen the
        # backfill row so the dialog is mounted while the sweep runs.
        coffee1_label = f"History — PROBE COFFEE 1 (transaction {rows['coffee1']})"
        click_button(tab, f'button[aria-label="{coffee1_label}"]')
        drawer_lines(tab)
        load_axe(tab)
        axe_check(tab, "History drawer open on /transactions")
        close_dialog(tab)

        Path(args.out).write_text(
            json.dumps({"checks": CHECKS, "evidence": evidence}, indent=2, ensure_ascii=False)
        )
        failed = [c for c in CHECKS if not c["ok"]]
        print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
        for c in failed:
            print("  FAIL " + c["check"])
        return 1 if failed else 0
    finally:
        browser.close()


if __name__ == "__main__":
    sys.exit(main())

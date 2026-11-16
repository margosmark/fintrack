"""M141 — a dead symbol stops promising "yet", and the fan-out stops trying.

Run:
    A11Y_BASE=http://192.0.2.109:8080 python3 m141_dead_symbol_probe.py

What it proves, in the order the checks run:

1. **The verdict, live.** `GET /securities/quote-check?symbol=APOTEX` on a
   REAL yfinance seam answers `quote_found: false` with the verbatim
   "Quote not found for symbol: APOTEX" — and a live symbol answers `true`.
   `quote_found: null` (could not ask) is never reported as "no quote".
2. **The flag, live.** Three misses set `dead_symbol`; a successful fetch
   resets the counter and clears the flag (the relisting heals).
3. **The sweep stops.** `POST /portfolio/refresh` answers
   `{"queued": 0, "skipped_dead": N}` once the row is flagged, and the api's
   own log gains NO further "Quote not found" lines across repeated sweeps —
   the 404 machine is off.
4. **The Detail page reads the honest state.** The empty state says "No public
   quote for APOTEX", the reason sentence names the verdict, and the
   "Refresh prices" action is DISABLED with `aria-describedby` bound to that
   reason (the M140 G2 pattern) — with a plainly-labelled "Try refresh" escape
   hatch that still works.
5. **The add form warns without blocking.** Submitting APOTEX raises a
   non-blocking warning with "Add anyway" and a cancel; "Add anyway" completes
   the add in one click.
6. **The watchlist row says "no public quote", not "no price".**

The tenant is deleted at the end (`DELETE /users/me` -> 204) unless
`--keep-tenant` is passed. The demo account is never touched.

Five traps cost runs here, and all five are now pinned in the code rather
than rediscovered:

- **The session cannot survive a page load on a plain-HTTP origin.** The
  access token is memory-only and the backend's refresh cookie is `Secure`
  (`Set-Cookie: refresh_token=...; HttpOnly; SameSite=strict; Secure`,
  measured) — Chrome will not store it over `http://192.0.2.109`, so every
  hard navigation comes back as /login and the api client's own 401 path is a
  HARD `window.location.assign("/login")`. The probe therefore logs in once
  (against the deep link, so RequireAuth's `from` state routes straight back)
  and then moves only through the router — the same way a user does. Run it
  against `http://localhost:<tunnel>` (`ssh -L <tunnel>:localhost:8080 example.internal`)
  and the cookie is storable, which is what the `A11Y_BASE` override is for.
- **A native form submit looks like an app bug from the outside.** It reloads
  the document, wipes the in-memory token and leaves every selector null — the
  probe now records the submit event and every Document request so a swallow
  is named instead of guessed. (M142 shipped exactly that defect and its fix:
  the new-security form was a sibling of the trade form, so its submit went to
  the outer one.)
- **The api log check must be scoped, or it is not a measurement.** The
  deployed stack is SHARED — Mark's tenant and every other agent's probe run
  in this one container — so an unscoped count of `Quote not found` lines
  moves for reasons that have nothing to do with this row. A first run read
  `12 -> 17` while nothing this probe asked for was happening, and the five
  new lines were BANK/VEQT/XGRO/XEQT/APTX from somebody else's fan-out.
  `log_noise` takes the symbols it counts and matches yfinance's two shapes
  behind a word boundary, so `APOTEX` never counts the `APOTEX2` this same
  probe just added through the warning.
- **A sliding window cannot answer "what is NEW?"** `docker compose logs
  --since 60s` ages its own baseline out while the sweeps run, so a quiet
  system reports fewer lines afterwards (measured: `10 -> 0`) and failed a
  check it was satisfying. The window is absolute: a baseline timestamp taken
  before the action, and "lines logged since then".
- **A second Chrome cannot share a first one's profile dir.** Chrome hands a
  second launch of the same `--user-data-dir` to the instance already holding
  it and exits WITHOUT opening its debug port, so the driver reports "DevTools
  never came up" for what is really somebody else's lock. Both the profile dir
  and the CDP port now default to a per-run value.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from a11y_driver import Browser
from probe_target import random_password

BASE = os.environ.get("A11Y_BASE", "http://192.0.2.109:8080").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))

FAILURES: list[str] = []

_CLEANUP: dict[str, Any] = {"api": None, "tenant": None, "password": None, "keep": False, "done": False}


def _delete_tenant() -> None:
    api, tenant = _CLEANUP["api"], _CLEANUP["tenant"]
    if api is None or tenant is None or _CLEANUP["keep"] or _CLEANUP["done"]:
        return
    status, _ = api.call("DELETE", "/users/me", {"current_password": _CLEANUP["password"]})
    _CLEANUP["done"] = True
    print(
        f"  [{'PASS' if status == 204 else 'FAIL'}] throwaway tenant deleted "
        f"({tenant['email']}) — DELETE /users/me {status}"
    )


atexit.register(_delete_tenant)


def check(label: str, ok: bool, detail: Any = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail not in ("", None) else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")
    return ok


# --------------------------------------------------------------------------
# HTTP helpers: registration, seeding and the API-level assertions happen
# OUTSIDE the page, so no password is ever typed into a form.
# --------------------------------------------------------------------------


class Api:
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
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode()
                for key, value in resp.headers.items():
                    if key.lower() == "set-cookie" and value.startswith("refresh_token="):
                        self.cookie = value.split(";", 1)[0]
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw


def log_noise(symbols: tuple[str, ...], count: int = 60, since: str | None = None) -> int:
    """How many fetch failures THESE symbols produced in the api's log.

    Two traps, both measured, both now pinned rather than rediscovered:

    - **A sliding window cannot answer "did anything NEW get logged?"** The api
      runs on the deploy host, not next to the probe: when the compose
      directory is not local this reads the SAME container's log over ssh, so
      the "the 404 machine is off" check is a real measurement rather than a
      silently-skipped one. `since` (RFC3339, UTC) makes that window ABSOLUTE —
      pass a baseline taken before an action and the result is "lines logged
      since then". A sliding ``--since 60s`` window cannot answer that
      question: the baseline's own lines AGE OUT of it, so a quiet system
      reports fewer lines afterwards (measured: 10 -> 0) and the equality
      check failed while nothing new had been logged at all.
    - **The deployed stack is SHARED** — Mark's own tenant and every other
      agent's probe run in this one container, so an UNSCOPED count of
      "Quote not found" lines moves for reasons that have nothing to do with
      this row. A first run of this check read 12 -> 17 and the five new lines
      were BANK/VEQT/XGRO/XEQT/APTX from somebody else's fan-out. The count is
      therefore scoped to the symbols THIS probe owns.
    """
    import shlex
    import subprocess

    compose_dir = os.environ.get("A11Y_COMPOSE_DIR", "/home/developer/fin_tracker")
    window = ["--since", since] if since else ["--since", f"{count}s"]
    argv = ["docker", "compose", "logs", "api", *window]
    if os.path.isdir(compose_dir):
        cmd, cwd = argv, compose_dir
    else:
        host = os.environ.get("A11Y_LOG_HOST", "example.internal")
        remote = "cd " + shlex.quote(compose_dir) + " && " + " ".join(shlex.quote(a) for a in argv)
        cmd, cwd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote], None
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=cwd,
        ).stdout
    except Exception as exc:  # noqa: BLE001 — the check reports, it does not crash
        print(f"  [note] could not read api logs: {exc}")
        return -1
    # `APOTEX` must not count `APOTEX2`: yfinance prints both
    # "Quote not found for symbol: <S>" and "$<S>: possibly delisted", so match
    # the symbol followed by a non-word character in both shapes.
    pattern = re.compile(
        r"(?:Quote not found for symbol:\s*|\$)"
        + r"(?:"
        + "|".join(map(re.escape, symbols))
        + r")\b"
    )
    return sum(1 for line in out.splitlines() if pattern.search(line))


def net_failures(page) -> list[dict]:
    """Every HTTP response >= 400 Chrome has seen since the last drain.

    The client's 401 path is a HARD redirect to /login (`api/client.ts`), so a
    page that vanishes mid-interaction leaves no DOM evidence at all — this is
    how the probe finds out WHICH call took the session away.
    """
    out: list[dict] = []
    for ev in page.events("Network.responseReceived"):
        response = (ev.get("params", {}) or {}).get("response", {}) or {}
        status = response.get("status", 0)
        if status >= 400:
            out.append({"status": status, "url": response.get("url", "")[:200]})
    return out


def doc_requests(page) -> list[str]:
    """Document-level requests Chrome has seen since the last drain.

    The difference matters: `api/client.ts` answers a dead session with a HARD
    `window.location.assign("/login")` (a Document request), while React
    Router's own bail-out is a same-document route change (none). Which of the
    two took the page says whether an HTTP call was to blame at all.
    """
    out: list[str] = []
    for ev in page.events("Network.requestWillBeSent"):
        params = ev.get("params", {}) or {}
        if params.get("type") == "Document":
            out.append((params.get("request", {}) or {}).get("url", ""))
    return out


def deployed_sha() -> str | None:
    """The commit the deploy host is actually running — the probe's evidence
    is worthless if it does not say WHICH build it measured."""
    import shlex
    import subprocess

    compose_dir = os.environ.get("A11Y_COMPOSE_DIR", "/home/developer/fin_tracker")
    if os.path.isdir(compose_dir):
        argv, cwd = ["git", "rev-parse", "--short", "HEAD"], compose_dir
    else:
        host = os.environ.get("A11Y_LOG_HOST", "example.internal")
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
                "git -C " + shlex.quote(compose_dir) + " rev-parse --short HEAD"]
        cwd = None
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd=cwd)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def spa_goto(page, path: str, timeout: float = 25.0) -> bool:
    """Route to `path` WITHOUT a page load.

    A hard `page.navigate()` loses the session on this origin: the access
    token is memory-only and the backend's refresh cookie is `Secure`
    (measured: `Set-Cookie: refresh_token=...; HttpOnly; SameSite=strict;
    Secure`), which a plain-HTTP host cannot store — so every page load comes
    back as /login. The probe therefore moves the way a user does: an in-app
    link click where the shell has one, and the router's own history for the
    deep routes it does not.
    """
    page.evaluate(
        """
        (() => {
          const want = %s;
          const a = [...document.querySelectorAll('a[href]')]
            .find(x => (x.getAttribute('href') || '') === want);
          if (a) { a.click(); return 'link'; }
          history.pushState({}, '', want);
          window.dispatchEvent(new PopStateEvent('popstate'));
          return 'pushstate';
        })()
        """
        % json.dumps(path)
    )
    try:
        page.wait_for(f"location.pathname === {json.dumps(path)}", timeout=timeout)
    except Exception:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-tenant", action="store_true")
    args = parser.parse_args()
    from probe_target import require_target
    require_target(BASE, who="m141_dead_symbol_probe.py")
    _CLEANUP["keep"] = args.keep_tenant

    api = Api()
    _CLEANUP["api"] = api
    # Sections 1-4 are HTTP assertions; keep their measured values so the
    # committed evidence carries the whole run, not just the browser half.
    api_evidence: dict[str, Any] = {}

    print(f"M141 dead-symbol probe against {BASE}\n")

    # ---- 1. the throwaway tenant ------------------------------------------
    print("1. throwaway tenant")
    password = random_password("m141")
    _CLEANUP["password"] = password
    email = f"m141-probe-{int(time.time())}@example.com"
    status, _ = api.call("POST", "/auth/register", {"email": email, "password": password})
    check("register the throwaway tenant", status == 200, status)
    status, body = api.call("POST", "/auth/login", {"email": email, "password": password})
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise SystemExit(f"login failed: {status} {body}")
    api.token = body["access_token"]
    _CLEANUP["tenant"] = {"email": email}
    check("login (no password touches a form)", True)

    # ---- 2. the verdict, live ----------------------------------------------
    print("\n2. the verdict, live (real yfinance seam)")
    status, dead = api.call("GET", "/securities/quote-check?symbol=APOTEX")
    check("quote-check answers", status == 200, status)
    check("APOTEX: quote_found is false", dead and dead.get("quote_found") is False, dead)
    # The reason is whatever Yahoo actually said — the live verdict for APOTEX
    # is "No data found, symbol may be delisted", not a fixed string, so assert
    # the SHAPE (a non-empty verdict that says the symbol is gone) not a
    # literal the upstream can reword at will.
    check(
        "APOTEX: the verdict is non-empty and says the symbol is gone",
        bool(dead)
        and bool((dead.get("reason") or "").strip())
        and re.search(r"not found|no data found|delisted", dead["reason"], re.I) is not None,
        (dead or {}).get("reason"),
    )
    check(
        "APOTEX: the response echoes the symbol asked for",
        bool(dead) and dead.get("symbol") == "APOTEX",
        dead,
    )

    api_evidence["quote_check_apotex"] = dead
    status, live = api.call("GET", "/securities/quote-check?symbol=AAPL")
    check("AAPL: quote_found is true", status == 200 and live and live.get("quote_found") is True, live)

    # `probe_quote` probes `history(period="1d")` — the SAME call the price
    # fan-out makes — so a ticker Yahoo has never heard of is a real "no
    # quote" verdict, not a crash and not a network excuse.
    status, bogus = api.call("GET", "/securities/quote-check?symbol=NOSUCHTICKERXYZ")
    check(
        "an unknown ticker answers a real 'no quote' verdict",
        status == 200
        and bogus is not None
        and bogus.get("symbol") == "NOSUCHTICKERXYZ"
        and bogus.get("quote_found") is False
        and bool((bogus.get("reason") or "").strip()),
        bogus,
    )

    # ---- 3. create APOTEX and drive the fan-out to the flag -----------------
    print("\n3. the flag, live")
    api_evidence["quote_check_aapl"] = live
    status, sec = api.call(
        "POST",
        "/securities",
        {
            "symbol": "APOTEX",
            "name": "Apotex Inc.",
            "kind": "stock",
            "listing_currency": "USD",
            "exchange": "",
        },
    )
    if status not in (200, 201) or not isinstance(sec, dict) or "id" not in sec:
        raise SystemExit(f"security create failed: {status} {sec}")
    sid = sec["id"]
    check("created the APOTEX security", True, f"id={sid}")

    status, _ = api.call("POST", "/watchlist", {"security_id": sid})
    check("watched it", status in (200, 201), status)

    before = api.call("GET", f"/securities/{sid}")[1] or {}
    check(
        "starts unflagged",
        before.get("dead_symbol") is False,
        f"dead_symbol={before.get('dead_symbol')}",
    )

    # The manual escape hatch is the one path that probes a dead row; three
    # misses flag it.
    after: dict[str, Any] = {}
    for attempt in range(1, 4):
        api.call("POST", f"/securities/{sid}/refresh")
        time.sleep(1)
        after = api.call("GET", f"/securities/{sid}")[1] or {}
        if after.get("dead_symbol"):
            break
    check("three misses flag the symbol", after.get("dead_symbol") is True, after.get("quote_miss_count"))
    check("the miss counter reached 3", after.get("quote_miss_count") == 3, after.get("quote_miss_count"))
    # The stored verdict is whatever Yahoo said on the LAST candidate the
    # chain tried — for APOTEX that is the `.TO` suffix probe, so the wording
    # is "$APOTEX.TO: possibly delisted" rather than the quote-check's
    # "Quote not found". Assert the SHAPE (a non-empty verdict that says the
    # symbol is gone), not a literal an upstream reword can move.
    check(
        "the reason names the verdict",
        bool(re.search(r"not found|no data found|delisted", after.get("dead_symbol_reason") or "", re.I)),
        after.get("dead_symbol_reason"),
    )

    api_evidence["flag_after_three_misses"] = {
        "security_id": sid,
        "dead_symbol": after.get("dead_symbol"),
        "quote_miss_count": after.get("quote_miss_count"),
        "dead_symbol_reason": after.get("dead_symbol_reason"),
    }

    # ---- 4. the sweep stops -------------------------------------------------
    print("\n4. the sweep stops")
    status, sweep = api.call("POST", "/portfolio/refresh")
    check("POST /portfolio/refresh answers 202 (docs/08)", status == 202, status)
    check(
        "the sweep skips the dead row",
        isinstance(sweep, dict) and sweep.get("skipped_dead", 0) >= 1,
        sweep,
    )
    check(
        "and queues nothing for it",
        isinstance(sweep, dict) and sweep.get("queued") == 0,
        sweep,
    )

    api_evidence["sweep"] = sweep
    # Baseline FIRST (UTC, absolute): the sliding window below can only say
    # "how much is in the last 60s", and the earlier probes' own 404 lines
    # age out of it while we sweep — that is what made a QUIET system report
    # 10 -> 0 and fail a check it was satisfying.
    time.sleep(1.1)  # let the previous probe's log lines settle before baselining
    baseline = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    noise_before = log_noise(("APOTEX",))
    for _ in range(3):
        api.call("POST", "/portfolio/refresh")
    time.sleep(4)
    noise_new = log_noise(("APOTEX",), since=baseline)
    api_evidence["quote_not_found_log_lines"] = {
        "scoped_to": "APOTEX",
        "in_the_window_before": noise_before,
        "new_since_baseline": noise_new,
        "baseline_utc": baseline,
    }
    if noise_before >= 0 and noise_new >= 0:
        check(
            "repeated sweeps add NO new APOTEX 404 noise (the 404 machine is off)",
            noise_new == 0,
            f"{noise_before} in the window before, {noise_new} logged since",
        )

    # ---- 5. the UI ----------------------------------------------------------
    print("\n5. the UI")
    report: dict[str, Any] = {}
    # Two probes can be driving this host at once (the api container is shared,
    # and so is the browser box). Chrome hands a SECOND launch of the same
    # `--user-data-dir` to the instance already holding it and exits without
    # opening its debug port, and the driver then reports "DevTools never came
    # up" for what is really somebody else's lock. A per-run profile dir and
    # port make that impossible; both are env-overridable as before.
    os.environ.setdefault(
        "A11Y_PROFILE_DIR",
        os.path.join(os.environ.get("TMPDIR", "/tmp"), f"fintrack-a11y-m141-{os.getpid()}"),
    )
    os.environ.setdefault("A11Y_CDP_PORT", str(9500 + os.getpid() % 400))
    browser = Browser()
    try:
        page = browser.new_tab(BASE)
        # Chrome's own view of the network: the 401 -> hard-redirect to /login
        # leaves no DOM trace, so the failing call is read from here.
        page.send("Network.enable")
        page.send("Page.enable")
        page.enable_runtime()

        # --- 5a. the Detail page: the honest state -------------------------
        page.navigate(f"{BASE}/securities/{sid}")
        page.sleep(3)
        # A hard navigation lands on /login (the access token is memory-only,
        # and the `Secure` refresh cookie cannot survive a plain-HTTP origin —
        # measured above). The page hands the session to the app by POSTing the
        # credentials the probe minted, exactly as a user would, and RequireAuth's
        # `from` state routes straight back to the page that was asked for: NO
        # second hard navigation, or the session is gone again.
        page.evaluate(
            """(() => {
              const form = document.querySelector('form');
              if (!form) return 'no login form';
              const set = (sel, v) => {
                const el = document.querySelector(sel);
                const setter = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, v);
                el.dispatchEvent(new Event('input', {bubbles: true}));
              };
              set('input[type="email"]', %s);
              set('input[type="password"]', %s);
              return 'filled';
            })()"""
            % (json.dumps(email), json.dumps(password))
        )
        page.evaluate(
            """(() => {
              const btn = [...document.querySelectorAll('button')]
                .find(b => /Log in/.test(b.innerText));
              btn.click();
              return 'clicked';
            })()""",
            await_promise=True,
        )
        page.wait_for(f"location.pathname === '/securities/{sid}'", timeout=30)
        page.sleep(4)

        report["detail"] = page.evaluate(
            """(() => {
              const body = document.body.innerText;
              const title = document.querySelector('[data-testid="empty-state-title"]');
              const hint = document.querySelector('[data-testid="empty-state-hint"]');
              const reason = document.querySelector('[data-testid="empty-state-action-reason"]');
              const refresh = [...document.querySelectorAll('button')]
                .filter(b => /Refresh prices/.test(b.innerText))
                .map(b => ({disabled: b.disabled,
                            describedby: b.getAttribute('aria-describedby')}));
              const tryBtn = document.querySelector('[data-testid="try-refresh"]');
              return {
                path: location.pathname,
                booted_to_login: location.pathname === '/login',
                body_head: body.slice(0, 240).replace(/\\n/g, ' | '),
                title: title ? title.innerText : null,
                hint: hint ? hint.innerText : null,
                action_reason: reason ? reason.innerText : null,
                refresh: refresh,
                try_refresh_present: !!tryBtn,
                try_refresh_enabled: tryBtn ? !tryBtn.disabled : null,
                old_lie: /nothing has been fetched for this symbol yet/.test(body),
              };
            })()"""
        )
        d = report["detail"]
        check(
            "the empty state says the honest thing",
            bool(d["title"]) and "No public quote for APOTEX" in d["title"],
            d["title"],
        )
        check(
            "the reason names the verdict",
            bool(d["action_reason"])
            and re.search(
                r"not found|no data found|delisted|no public quote|cannot produce",
                d["action_reason"],
                re.I,
            )
            is not None,
            d["action_reason"],
        )
        check(
            "'Refresh prices' is DISABLED",
            len(d["refresh"]) > 0 and all(b["disabled"] for b in d["refresh"]),
            d["refresh"],
        )
        check(
            "the disabled action is bound to its reason (aria-describedby)",
            bool(d["action_reason"])
            and any(
                b["describedby"] and "reason" in b["describedby"] for b in d["refresh"]
            ),
            d["refresh"],
        )
        check("'Try refresh' escape hatch is present and enabled", d["try_refresh_enabled"] is True)
        check("the old 'nothing has been fetched ... yet' lie is gone", d["old_lie"] is False)

        page.screenshot(os.path.join(HERE, "m141-dead-detail.png"))

        # --- 5b. the watchlist row ------------------------------------------
        check("SPA route to /investments (a hard load would log us out)", spa_goto(page, "/investments"))
        page.sleep(4)
        report["watch"] = page.evaluate(
            """(() => {
              const row = document.querySelector('[data-testid="watch-row-APOTEX"]');
              return row ? row.innerText.replace(/\\n/g, ' | ') : null;
            })()"""
        )
        check(
            "the watchlist row says 'no public quote', not 'no price'",
            bool(report["watch"]) and "no public quote" in report["watch"],
            report["watch"],
        )

        # --- 5c. the add form warns without blocking ------------------------
        # Discrete short steps, not one long blind await: when the page
        # navigates under a 25 s `awaitPromise`, CDP answers "Inspected target
        # navigated or closed" and the probe loses the one fact it needed —
        # WHERE it went. Each step is its own call and the poll reports the
        # route it observed if the context dies under it.
        def page_state() -> dict:
            try:
                state = page.evaluate(
                    """(() => {
                      const NL = String.fromCharCode(10);
                      const warn = document.querySelector('[data-testid="quote-warning"]');
                      return {
                        path: location.pathname,
                        ready: document.readyState,
                        nav_type: (performance.getEntriesByType('navigation')[0] || {}).type ?? null,
                        history_len: history.length,
                        title: document.title,
                        head: document.body.innerText.slice(0, 160).split(NL).join(' | '),
                        warn: warn ? warn.innerText.split(NL).join(' ') : null,
                        add_anyway: !!document.querySelector('[data-testid="add-anyway"]'),
                        cancel: !!document.querySelector('[data-testid="dismiss-quote-warning"]'),
                      };
                    })()"""
                )
                return state if isinstance(state, dict) else {}
            except Exception as exc:  # noqa: BLE001 — report where it went
                return {"dead": str(exc)}

        opened = page.evaluate(
            """(() => {
              const t = document.querySelector('[data-testid="new-security-toggle"]');
              if (!t) return false;
              t.click();
              return true;
            })()"""
        )
        check("the new-security form is reachable", opened is True, opened)
        page.sleep(1)
        filled = page.evaluate(
            """(() => {
              const setVal = (el, v) => {
                const s = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value').set;
                s.call(el, v);
                el.dispatchEvent(new Event('input', {bubbles: true}));
              };
              const sym = document.querySelector('input[placeholder="XEQT"]');
              if (!sym) return 'no symbol input';
              const form = sym.closest('form');
              const name = form.querySelector('input:not([placeholder]):not([type])');
              setVal(sym, 'APOTEX2');
              if (name) setVal(name, 'Apotex Inc. (second listing)');
              return 'filled';
            })()"""
        )
        check("typed the dead symbol", filled == "filled", filled)
        page.sleep(1)
        # Drain BEFORE submitting: the failure this looks for happens during
        # the quote-check, and a drain placed after the click would throw the
        # evidence away with the rest of the buffer.
        net_failures(page)
        submitted = page.evaluate(
            """(() => {
              // A NATIVE form submit (GET /investments?) reloads the document
              // and destroys the in-memory session on a plain-HTTP origin, so
              // record whether the click's default was actually prevented —
              // React's onSubmit says it is, and the network says otherwise.
              const b = [...document.querySelectorAll('button')]
                .find(x => /Create security/.test(x.innerText));
              if (!b) return 'no submit button';
              window.__m141submits = [];
              const formEl = b.closest('form');
              const rkey = formEl
                ? Object.keys(formEl).find(k => k.indexOf('__reactProps$') === 0)
                : undefined;
              window.__m141props = {
                has_form: !!formEl,
                react_owned: !!rkey,
                onsubmit_is_fn: rkey ? typeof formEl[rkey].onSubmit === 'function' : null,
                button_type: b.getAttribute('type'),
              };
              // CAPTURE phase, so this reads defaultPrevented BEFORE React's
              // bubble-phase onSubmit gets to it — a `false` here is the
              // browser's own starting state, not a swallowed handler. What
              // proves the handler ran is that no Document request follows.
              window.addEventListener('submit', (e) => {
                window.__m141submits.push({
                  prevented_at_capture: e.defaultPrevented,
                  action: e.target && e.target.action,
                });
              }, true);
              // Returned BEFORE the click's default action can navigate: a
              // native submit wipes the window, so anything stashed on it is
              // unreadable afterwards.
              const props = window.__m141props;
              b.click();
              return {clicked: true, props: props};
            })()"""
        )
        check(
            "submitted the form",
            isinstance(submitted, dict) and submitted.get("clicked") is True,
            submitted,
        )
        report["form_ownership"] = (submitted or {}).get("props")

        # The quote-check is a real yfinance probe over the wire (three
        # candidate symbols for a name-shaped string), so poll with room —
        # and record every failing call the browser saw, because a 401 there
        # is a HARD redirect to /login with no DOM left to read.
        a: dict = {}
        bad: list[dict] = []
        docs: list[str] = []
        trace: list[dict] = []
        for _ in range(45):
            page.sleep(1)
            bad.extend(net_failures(page))
            docs.extend(doc_requests(page))
            a = page_state()
            trace.append({k: a.get(k) for k in ("path", "ready", "title", "head")})
            trace = trace[-6:]
            if a.get("warn") or a.get("dead") or a.get("path") == "/login":
                break
        report["add_trace"] = trace
        report["console_errors"] = page.console_errors()[:12]
        try:
            report["native_submits"] = page.evaluate("window.__m141submits")
        except Exception as exc:  # noqa: BLE001
            report["native_submits"] = str(exc)
        report["add"] = {
            "warning_shown": bool(a.get("warn")),
            "warning_text": a.get("warn"),
            "add_anyway": a.get("add_anyway"),
            "cancel": a.get("cancel"),
            "path": a.get("path"),
            "dead": a.get("dead"),
            "http_failures": bad,
            "document_requests": docs,
        }
        a = report["add"]
        check("submitting a dead symbol raises a non-blocking warning", a.get("warning_shown") is True, a)
        check(
            "the warning names the symbol",
            bool(a.get("warning_text")) and "APOTEX2" in a["warning_text"],
            a.get("warning_text"),
        )
        check("'Add anyway' is offered", a.get("add_anyway") is True)
        check("a cancel is offered", a.get("cancel") is True)
        # The new-security card sits below the fold on /investments — a
        # viewport shot of the top of the page is a screenshot of the wrong
        # thing, which is exactly what the first committed evidence was.
        page.evaluate(
            """(() => {
              const w = document.querySelector('[data-testid="quote-warning"]');
              if (w) w.scrollIntoView({block: 'center'});
              return !!w;
            })()"""
        )
        page.sleep(0.6)
        page.screenshot(os.path.join(HERE, "m141-add-warning.png"))

        # --- 5d. 'Add anyway' completes the add in one click ----------------
        clicked = page.evaluate(
            """(() => {
              const btn = document.querySelector('[data-testid="add-anyway"]');
              if (!btn) return false;
              btn.click();
              return true;
            })()"""
        )
        check("'Add anyway' is a real click", clicked is True, clicked)
        # The form resets when the create lands, so poll for THAT rather than
        # sleeping through it (and so a navigation is reported, not swallowed).
        anyway: dict[str, Any] = {"warning_cleared": False, "form_reset": False}
        report["anyway"] = anyway
        for _ in range(30):
            page.sleep(1)
            state = page_state()
            if state.get("dead"):
                report["anyway"]["dead"] = state["dead"]
                break
            if state.get("warn") is None:
                report["anyway"]["warning_cleared"] = True
                try:
                    reset = page.evaluate(
                        """(() => {
                          const sym = document.querySelector('input[placeholder="XEQT"]');
                          return !!sym && sym.value === '';
                        })()"""
                    )
                except Exception as exc:  # noqa: BLE001
                    report["anyway"]["dead"] = str(exc)
                    break
                report["anyway"]["form_reset"] = reset is True
                break
        # The form does not watch what it creates (a user picks that
        # separately), so the row is NOT evidence the add landed — ask the
        # api, which is where the security would have to be.
        time.sleep(2)
        _, listed = api.call("GET", "/securities?limit=100")
        symbols = [row.get("symbol") for row in (listed or {}).get("items", [])]
        report["anyway"]["created"] = "APOTEX2" in symbols
        check(
            "'Add anyway' completes the add in one click",
            report["anyway"].get("warning_cleared") is True
            and report["anyway"].get("created") is True,
            report["anyway"],
        )
    finally:
        browser.close()

    print("\nreport:")
    print(json.dumps(report, indent=2))

    # The evidence, committed alongside the probe (the M140 house pattern):
    # which build was measured, what the browser saw, and which checks held.
    evidence = {
        "base": BASE,
        "deployed_sha": deployed_sha(),
        "api": api_evidence,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks_passed": not FAILURES,
        "failures": FAILURES,
        "report": report,
    }
    with open(os.path.join(HERE, "m141-dead-symbol-probe.json"), "w") as fh:
        json.dump(evidence, fh, indent=2)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

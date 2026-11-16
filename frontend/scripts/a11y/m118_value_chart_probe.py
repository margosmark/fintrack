"""M118 — the live probe for the account value chart (AGENTS §4 step 5).

    python3 m118_value_chart_probe.py [--base http://localhost:8080]
                                     [--account 1]

**Defaults are NON-PRODUCTION (card t_355d54e3).** The default target is the
local compose path (`http://localhost:8080`, classified LOCAL by
`probe_target.py`), and every marker default describes the SYNTHETIC fixture
below — invented account, invented dates/amounts. A production run must pass
`--base`, `--account` and all four marker args explicitly AND export
`PROBE_ALLOW_PROD=1`; without the flag `require_target` exits 1 before any
tab is opened (fail-closed — see frontend/scripts/a11y/probe_target.py).

**Re-running against the synthetic fixture** (how the committed PNG/JSON
evidence was regenerated): stand up a local stack (backend uvicorn + `npx
vite`, whose vite.config.ts proxies `/api`), register a disposable
`@example.com` tenant, create one investment account plus a `contribution`
cash trade on `--deposit` for `--deposit-amount` and a `withdrawal` cash
trade on `--withdrawal` for `--withdrawal-amount`, mint the owner token with
the app's own `create_access_token(<user id>)` into `--token-file`, then run
this script with `--base http://localhost:5173`. Nothing on that path reaches
production.

**Auth (read this first).** Mark's password is not ours to use, and this
browser has no live session either (checked: `POST /auth/refresh` answers
`401 invalid_or_revoked_refresh_token` here). So the probe carries its OWN
access token: minted by the operator with the app's own
`create_access_token(<owner>)` (inside the api container for a deployed
stack, from the backend venv for a local one; 15-minute TTL,
written to `--token-file`, never printed), and PROVEN good by a real
`GET /users/me` before anything is faked. The app's `POST /auth/refresh` is
then answered in-browser over CDP `Fetch.respondWith`, so that one request
never reaches the server — no `refresh_tokens` row is written, rotated or
revoked and no other session of his is disturbed — while every request after
it is the app's own, sent with that token to the real backend. The token file
is deleted at the end and the probe writes NOTHING else to the tenant: every
check is a GET or a UI interaction. It drives the PERSISTENT headed Chrome on
127.0.0.1:9333 and closes its tab on the way out.

What each check proves:

  1 live      the deployed route answers (status + `Cache-Control`) off the
              app's OWN request, and `/accounts/<account>` renders the chart
              module
  2 series    `has_data`, <= 400 buckets, and `cash_cad + positions_cad ==
              value_cad` at every valued bucket (the hand-checkable identity
              docs/08 pins — each cell is quantized to cents before the sum)
  3 markers   BOTH marker dates are on the wire with the right sign:
              `--deposit` carries `+<--deposit-amount>` contribution, and
              `--withdrawal` carries `<--withdrawal-amount>` — a NEGATIVE
              amount, which docs/08 renders as a withdrawal. The defaults are
              the synthetic fixture (card t_355d54e3), never a real account.
  4 chart     the `role="img"` sentence carries the series facts, and there is
              exactly one SVG `<title>` pin per wire event — markers are
              DERIVED, never a second list that could drift
  5 pin       hovering the deposit pin shows `+ $<deposit-amount> contribution`
              → screenshot (the acceptance shot)
  6 jump      clicking that pin filters the Trades log to `contribution` + the
              cash security, scrolls to the row and rings it `aria-current` —
              cause → evidence. A cash-only account has ZERO transactions, so
              its markers land in the trades table, which is where its money
              is
  7 table     the `<details>` data table lists the same markers with a
              keyboard-reachable `Show` button
  8 axe       0 violations at 390/412/1440 in BOTH themes (6 runs)
  9 console   no console errors during the whole run

Exit 0 only if every check passes; JSON + PNG evidence land next to this script.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

import websocket

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from axe_scan import load_axe, run_axe, set_theme, summarize  # noqa: E402

CDP_BASE = "http://127.0.0.1:9333"
CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


def http(path: str, method: str = "GET"):
    req = urllib.request.Request(CDP_BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read()
    if not body.strip():
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


class Tab:
    """One CDP page session: the message loop is a11y_driver.CDPSession's,
    with `suppress_origin` (the headed browser only allows its own origins)."""

    def __init__(self, ws_url: str, refresh_body: bytes | None = None):
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self._id = 0
        self._pending: dict[str, list[dict]] = {}
        self.console_errors: list[str] = []
        # A synthetic POST /auth/refresh answer — see the module docstring.
        # None = no interception (the normal, cookie-driven path).
        self._refresh_body = refresh_body
        self.refresh_answers = 0
        self.paused_seen = 0
        # ids WE issued from inside an event handler — their replies arrive
        # while some other command is in flight and must not be swallowed
        # silently (an interception error would otherwise hang the page with
        # no trace).
        self._own_ids: set[int] = set()
        self.intercept_errors: list[str] = []

    def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            if not isinstance(raw, str):
                continue
            data = json.loads(raw)
            if "id" in data and data["id"] in self._own_ids:
                self._own_ids.discard(data["id"])
                if "error" in data:
                    self.intercept_errors.append(json.dumps(data["error"])[:300])
                continue
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})
            if "method" in data:
                self._handle_event(data)

    def _handle_event(self, data: dict) -> None:
        """Buffer an event for later drains, and answer the ones that cannot
        wait (a paused request would hang the page's fetch while we think)."""
        if data.get("method") == "Fetch.requestPaused":
            self.paused_seen += 1
            if self._refresh_body is not None:
                self._answer_refresh(data.get("params") or {})
                return
        key = data.get("sessionId") or "browser"
        self._pending.setdefault(f"{key}|{data['method']}", []).append(data)
        self._note(data)

    def _answer_refresh(self, params: dict) -> None:
        """Fulfill the app's POST /auth/refresh with the probe's own access
        token (see the module docstring). The request never reaches the
        server, so nothing in the tenant's session table is touched."""
        self.refresh_answers += 1
        self._id += 1
        self._own_ids.add(self._id)
        self.ws.send(json.dumps({
            "id": self._id,
            "method": "Fetch.fulfillRequest",
            "params": {
                "requestId": params.get("requestId"),
                "responseCode": 200,
                "responseHeaders": [
                    {"name": "Content-Type", "value": "application/json"},
                    {"name": "Cache-Control", "value": "no-store"},
                ],
                "body": base64.b64encode(self._refresh_body or b"{}").decode(),
            },
        }))

    def _note(self, data: dict) -> None:
        """Collect console/page errors for check 9."""
        method = data.get("method")
        params = data.get("params") or {}
        if method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            args = " ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", []))
            self.console_errors.append(args[:300])
        elif method == "Runtime.exceptionThrown":
            exc = params.get("exceptionDetails", {})
            self.console_errors.append(str(exc.get("text", "exception"))[:300])
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            if entry.get("level") == "error":
                self.console_errors.append(str(entry.get("text", ""))[:300])

    def pump(self, seconds: float = 0.3) -> None:
        """Read whatever the browser has sent for a moment.

        Events only arrive while we are reading the socket, so a wait loop
        that ONLY inspects its buffer would starve — this is the pump.
        """
        self.ws.settimeout(seconds)
        try:
            while True:
                raw = self.ws.recv()
                if not isinstance(raw, str):
                    continue
                data = json.loads(raw)
                if "method" in data:
                    self._handle_event(data)
        except (websocket.WebSocketTimeoutException, TimeoutError, OSError):
            pass
        finally:
            self.ws.settimeout(60)

    def drain(self, key_suffix: str) -> list[dict]:
        out: list[dict] = []
        for key in [k for k in self._pending if k.endswith(key_suffix)]:
            out.extend(self._pending.pop(key))
        return out

    def evaluate(self, expr: str, await_promise: bool = False):
        # Expression goes through UNWRAPPED — the same contract
        # a11y_driver.Tab.evaluate has, so `load_axe`'s whole bundle and
        # `axe.run(...)` both evaluate exactly as they do there.
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        details = result.get("exceptionDetails")
        if details:
            raise RuntimeError(details.get("exception", {}).get("description") or details.get("text"))
        return result.get("result", {}).get("value")

    def wait_for(self, expr: str, timeout: float = 25) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(expr):
                    return
            except Exception:
                pass
            time.sleep(0.25)
        raise TimeoutError(f"condition never held: {expr[:80]}")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def force_viewport(self, width: int, height: int) -> int:
        # `mobile: False` is the house setting (a11y_driver.force_viewport):
        # the width must be what the page REALLY got, never a requested value.
        self.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
        })
        self.sleep(0.4)
        return int(self.evaluate("window.innerWidth") or 0)

    def navigate(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})
        self.wait_for("document.readyState === 'complete'", timeout=30)

    def screenshot(self, path: str) -> None:
        data = self.send("Page.captureScreenshot", {"format": "png"})
        Path(path).write_bytes(base64.b64decode(data["data"]))
        print(f"      shot → {os.path.basename(path)}", flush=True)

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def build_refresh_body(base: str, token_file: str) -> bytes:
    """The exact body a successful `POST /auth/refresh` returns
    (`TokenResponse`: `{user, access_token, token_type}`), assembled from the
    probe's own access token and the REAL `GET /users/me` answer for it.

    The token is minted by the operator (`create_access_token(<owner>)` inside
    the api container, written to `--token-file`, never printed) because this
    browser has no live refresh cookie for this origin: `GET /users/me` proves
    the token works BEFORE anything is faked, and the refresh request itself
    is answered in-browser so the tenant's session table is never touched."""
    token = Path(token_file).read_text().strip()
    req = urllib.request.Request(
        f"{base}/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        user = json.loads(resp.read())
    if not isinstance(user, dict) or "id" not in user:
        raise RuntimeError("GET /users/me did not answer a user — mint the token again")
    return json.dumps({"user": user, "access_token": token, "token_type": "bearer"}).encode()


def fetch_value_history(base: str, account: int, token_file: str) -> dict:
    """The endpoint's contract, read with the probe's own token.

    The app's OWN request is asserted separately from the page's resource
    timing (`app asked`) — that is the end-to-end signal — while the status,
    header and payload checks come from here, so they cannot be swayed by how
    the browser chose to report a service-worker-mediated fetch.
    """
    token = Path(token_file).read_text().strip()
    req = urllib.request.Request(
        f"{base}/api/v1/accounts/{account}/value-history",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return {
            "status": resp.status,
            "headers": dict(resp.headers),
            "url": req.full_url,
            "payload": json.loads(resp.read()),
        }


def open_tab(url: str, refresh_body: bytes | None = None) -> tuple[Tab, str]:
    target = http(f"/json/new?{urllib.parse.quote(url, safe='')}", method="PUT")
    if not target or "id" not in target:
        raise RuntimeError("could not open a tab in the headed browser (is 9333 up?)")
    target_id = target["id"]
    ws_url = target.get("webSocketDebuggerUrl") or http("/json")["webSocketDebuggerUrl"]
    # /json/new returns the target; re-read its ws url from the list (stable).
    for t in http("/json"):
        if t.get("id") == target_id:
            ws_url = t["webSocketDebuggerUrl"]
    tab = Tab(ws_url, refresh_body)
    for method in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
        tab.send(method)
    # The chart endpoint is cacheable (`private, max-age=60`), and a response
    # replayed from the HTTP cache emits no `Network.responseReceived` — which
    # would make "did the app ask for the chart?" depend on how recently the
    # last run loaded the page. Disable the cache so every run observes the
    # real request (the header under test still arrives from the server).
    tab.send("Network.setCacheDisabled", {"cacheDisabled": True})
    if refresh_body is not None:
        # BEFORE the first navigation: the app's session probe is the very
        # first request it makes, and a paused one would hang its boot.
        tab.send("Fetch.enable", {"patterns": [{"urlPattern": "*auth/refresh*"}]})
    return tab, target_id


def close_tab(target_id: str) -> None:
    try:
        http(f"/json/close/{target_id}", method="PUT")
    except Exception:
        pass


def settle(tab: Tab, extra: float = 1.6) -> None:
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"route-fallback\"]') === null"
            " && document.querySelector('main') !== null",
            timeout=30,
        )
    except TimeoutError:
        pass
    tab.sleep(extra)


def pin_list(tab: Tab) -> list[dict]:
    """Every MARKER pin: its `<title>` text and its on-screen centre.

    Scoped by the heading shape (`+ $<amount> contribution`) because other
    charts on this page — donut slices, sparklines — also carry `<title>`
    elements with circles under them, and counting those would make "one pin
    per wire event" fail for the wrong reason.
    """
    return tab.evaluate(
        r"""(() => [...document.querySelectorAll('svg title')]
            .filter(t => /^[+\u2212]\s*\$/.test((t.textContent || '').trim()))
            .map(t => {
            const c = t.parentElement && t.parentElement.querySelector('circle');
            if (!c) return null;
            const r = c.getBoundingClientRect();
            return {text: (t.textContent || '').trim(),
                    x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width};
          }).filter(Boolean))()"""
    ) or []


def mouse(tab: Tab, kind: str, x: float, y: float) -> None:
    """Trusted mouse input. `button`/`buttons` must follow the event's own
    semantics — sending `button: "left"` on a MOVE tells Chrome a button is
    HELD, which turns the next press into a drag and the pin never sees a
    click (measured: the hover worked, the click never did)."""
    if kind == "mouseMoved":
        button, buttons, click_count = "none", 0, 0
    elif kind == "mousePressed":
        button, buttons, click_count = "left", 1, 1
    else:  # mouseReleased
        button, buttons, click_count = "left", 0, 1
    tab.send("Input.dispatchMouseEvent", {
        "type": kind, "x": x, "y": y, "button": button,
        "buttons": buttons, "clickCount": click_count,
    })


def wait_for_value_history(tab: Tab, timeout: float = 30.0) -> dict | None:
    """The app's OWN request for the chart — captured off the wire, so the
    check reads the status and headers the browser really received.

    `Network.responseReceived` fires when the HEADERS land, before the body has
    finished streaming, and `Network.getResponseBody` refuses until
    `Network.loadingFinished` — so the request id is remembered and the body is
    retried every tick instead of being read once and dropped.
    """
    deadline = time.time() + timeout
    request_id: str | None = None
    meta: dict = {}
    seen = 0
    requested: str | None = None
    while time.time() < deadline:
        for event in tab.drain("|Network.requestWillBeSent"):
            url = (event.get("params", {}).get("request", {}) or {}).get("url") or ""
            if "value-history" in url:
                requested = event.get("requestId")
        for event in tab.drain("|Network.responseReceived"):
            seen += 1
            response = event.get("response", {})
            if "value-history" in (response.get("url") or ""):
                request_id = event["requestId"]
                meta = response
        if os.environ.get("M118_DEBUG"):
            print(f"   [wait] {seen} responses, request_id={request_id} "
                  f"requested={requested}", flush=True)
        if request_id is not None:
            try:
                body = tab.send("Network.getResponseBody", {"requestId": request_id})
            except RuntimeError:
                body = None  # still streaming (or already evicted) — retry
            if body is not None:
                try:
                    payload = json.loads(body.get("body") or "{}")
                except json.JSONDecodeError:
                    payload = None
                if payload is not None:
                    return {
                        "status": meta.get("status"),
                        "headers": meta.get("headers", {}),
                        "url": meta.get("url"),
                        "payload": payload,
                    }
        tab.pump(0.3)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    # Defaults = local target + the synthetic fixture (card t_355d54e3).
    # Production requires an explicit --base/--account/marker args AND
    # PROBE_ALLOW_PROD=1 (require_target below exits before any tab opens).
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--account", type=int, default=1)
    ap.add_argument("--deposit", default="2026-01-12")
    ap.add_argument("--withdrawal", default="2026-02-16")
    ap.add_argument("--deposit-amount", default="12500.00")
    ap.add_argument("--withdrawal-amount", default="-4320.75")
    ap.add_argument("--out", default=str(HERE / "m118-value-chart-probe.json"))
    ap.add_argument("--token-file", default="/tmp/.m118-token")
    args = ap.parse_args()
    from probe_target import require_target
    require_target(args.base, who="m118_value_chart_probe.py")

    account = args.account
    url = f"{args.base}/accounts/{account}"
    refresh_body = build_refresh_body(args.base, args.token_file)
    tab, target_id = open_tab("about:blank", refresh_body)
    print(f"probe tab {target_id} → {url}", flush=True)
    try:
        # --- live + series (the app's own request, headers included) --------
        tab.navigate(url)
        settle(tab)
        landed = tab.evaluate("location.pathname")
        check("session", landed == f"/accounts/{account}",
              f"landed on {landed}" + ("" if landed == f"/accounts/{account}" else " — NOT signed in"))

        # 1. the APP asked for its own chart — resource timing is the
        # end-to-end signal that the feature is wired into the page, and it
        # does not depend on how the browser reports a fetch to CDP. The query
        # fires once the account has loaded, so wait for it rather than racing.
        app_asks = 0
        for _ in range(60):
            app_asks = int(
                tab.evaluate(
                    "performance.getEntriesByType('resource')"
                    ".filter(e => e.name.includes('value-history')).length"
                )
                or 0
            )
            if app_asks >= 1:
                break
            tab.sleep(0.5)
        chart_mounted = bool(tab.evaluate("!!document.querySelector('[role=img]')"))
        check("app asked", app_asks >= 1 and chart_mounted,
              f"{app_asks} value-history request(s) from the page, chart frame rendered={chart_mounted}")

        # Bonus observation, not a gate: hand CDP a chance to hand over the
        # app's own response (a service-worker-mediated fetch may not surface
        # on the page's session, which is why the contract check below reads
        # the route directly instead).
        wire_seen = wait_for_value_history(tab, timeout=6)

        # The contract itself (status, header, payload) is read directly with
        # the probe's own token — same route, same server, same headers.
        wire = fetch_value_history(args.base, account, args.token_file)
        check("live-route", wire["status"] == 200,
              f"status={wire['status']} url={wire['url']}")
        if wire["status"] != 200:
            report = {"checks": CHECKS}
            Path(args.out).write_text(json.dumps(report, indent=1))
            return 1

        cache = {k.lower(): v for k, v in wire["headers"].items()}
        check("cache-header", cache.get("cache-control") == "private, max-age=60",
              str(cache.get("cache-control")))

        body = wire["payload"]
        buckets = body.get("buckets", [])
        events = body.get("events", [])
        check("series", body.get("has_data") is True and 0 < len(buckets) <= 400,
              f"has_data={body.get('has_data')} buckets={len(buckets)} "
              f"granularity={body.get('granularity')}/{body.get('requested_granularity')} "
              f"from={body.get('from')} to={body.get('to')}")

        bad = [
            b for b in buckets
            if b.get("valued")
            and b.get("cash_cad") is not None
            and b.get("positions_cad") is not None
            and round(float(b["cash_cad"]) + float(b["positions_cad"]), 2) != round(float(b["value_cad"]), 2)
        ]
        check("cash+positions==value", not bad,
              f"{len(buckets) - len(bad)}/{len(buckets)} buckets satisfy the identity")

        by_date = {e["date"]: e for e in events}
        dep = by_date.get(args.deposit)
        wit = by_date.get(args.withdrawal)
        dep_amount = float(args.deposit_amount)
        wit_amount = float(args.withdrawal_amount)
        # The tooltip/pin heading the app renders (`+ $12,500.00 contribution`).
        dep_label = f"${Decimal(args.deposit_amount):,.2f}"
        check("marker deposit", bool(dep) and dep["kind"] == "contribution"
              and float(dep["amount_cad"]) == dep_amount and dep["direction"] == 1,
              f"{args.deposit} → {dep['kind'] if dep else 'MISSING'} "
              f"{dep['amount_cad'] if dep else ''}")
        check("marker withdrawal", bool(wit) and wit["kind"] == "withdrawal"
              and float(wit["amount_cad"]) == wit_amount and wit["direction"] == -1,
              f"{args.withdrawal} → {wit['kind'] if wit else 'MISSING'} "
              f"{wit['amount_cad'] if wit else ''}")

        # --- chart frame + pins ---------------------------------------------
        label = tab.evaluate(
            "document.querySelector('[role=img]') && document.querySelector('[role=img]').getAttribute('aria-label')"
        ) or ""
        check("chart aria", "Account value over time" in label and "point" in label, label[:160])
        check("scroll region", tab.evaluate(
            "!!document.querySelector('div[role=region][tabindex=\"0\"].overflow-x-auto')"
        ), "labelled + focusable overflow box (M102)")

        pins = pin_list(tab)
        check("pins == events", len(pins) == len(events),
              f"{len(pins)} svg pins for {len(events)} wire events")

        # --- 5: hover the deposit pin, screenshot ----------------------------
        target = next(
            (p for p in pins if dep_label in p["text"] and "contribution" in p["text"]),
            None,
        )
        pin_detail = target["text"] if target else (pins[0]["text"] if pins else "no pins")
        check("deposit pin", target is not None, pin_detail)
        if target:
            tab.evaluate(
                "document.querySelector('figure').scrollIntoView({block:'center'})"
            )
            tab.sleep(0.5)
            pins = pin_list(tab)
            target = next((p for p in pins if dep_label in p["text"]), target)
            tab.force_viewport(1440, 900)
            set_theme(tab, "dark")
            tab.sleep(0.4)
            tab.evaluate(
                "document.querySelector('figure').scrollIntoView({block:'center'})"
            )
            tab.sleep(0.4)
            pins = pin_list(tab)
            target = next((p for p in pins if dep_label in p["text"]), None)
            if target:
                mouse(tab, "mouseMoved", target["x"], target["y"])
                tab.sleep(0.8)
                tip = tab.evaluate(
                    "(() => {const t = document.querySelector('.glass-strong');"
                    " return t ? t.innerText : '';})()"
                ) or ""
                check("pin tooltip", f"+ {dep_label} contribution" in tip.replace("\n", " "),
                      " / ".join(tip.splitlines())[:150])
                tab.screenshot(str(HERE / "m118-value-pin-1440-dark.png"))

                # --- 6: click it → the Trades log narrows + rings the row ----
                # Move OFF the pin first: the tooltip that the hover just drew
                # sits above the chart and would swallow the click (measured —
                # the pin never received it), and it hides on pointer-leave.
                mouse(tab, "mouseMoved", 8, 8)
                tab.sleep(0.5)
                mouse(tab, "mousePressed", target["x"], target["y"])
                mouse(tab, "mouseReleased", target["x"], target["y"])
                # The Trades log re-queries (action + security) and the row only
                # exists once that page lands — poll rather than guess a delay.
                jumped = None
                for _ in range(25):
                    tab.sleep(0.4)
                    jumped = tab.evaluate(
                        """(() => {
                          const row = document.querySelector('[data-trade-id][aria-current="true"]');
                          const action = document.querySelector('select[aria-label="Filter trades by action"]');
                          const security = document.querySelector('select[aria-label="Filter trades by security"]');
                          return {row: row ? row.getAttribute('data-trade-id') : null,
                                  text: row ? row.innerText.replace(/\\s+/g, ' ').slice(0, 160) : null,
                                  action: action ? action.value : null,
                                  security: security ? security.value : null,
                                  rows: document.querySelectorAll('[data-trade-id]').length,
                                  current: document.querySelectorAll('[aria-current="true"]').length};
                        })()"""
                    )
                    if jumped and jumped.get("row") and jumped.get("action") == "contribution":
                        break
                check("marker jump", bool(jumped) and jumped.get("row")
                      and jumped.get("action") == "contribution",
                      json.dumps(jumped)[:220] if jumped else "no answer")
                tab.screenshot(str(HERE / "m118-value-jump-1440-dark.png"))

        # --- 7: the data table ---------------------------------------------
        opened = tab.evaluate(
            """(() => { const d = document.querySelector('details');
                 if (d && !d.open) d.open = true;
                 const rows = [...document.querySelectorAll('details table')]
                    .map(t => t.innerText.replace(/\\s+/g, ' '));
                 const btn = document.querySelector('details button[aria-label^="Show the"]');
                 return {open: !!(d && d.open), tables: rows.length,
                         hasButton: !!btn,
                         buttonLabel: btn ? btn.getAttribute('aria-label') : null,
                         mentionsDeposit: rows.join(' ').includes('"""
                 + args.deposit
                 + """')};})()"""
        )
        check("data table", bool(opened) and opened.get("hasButton") and opened.get("mentionsDeposit"),
              json.dumps(opened)[:200])

        # --- 8: axe at three widths, both themes -----------------------------
        load_axe(tab)
        axe_runs = []
        for width in (390, 412, 1440):
            got = tab.force_viewport(width, 900)
            for theme in ("dark", "light"):
                set_theme(tab, theme)
                tab.sleep(0.5)
                result = run_axe(tab)
                summary = summarize(result.get("violations", []))
                axe_runs.append({"width": width, "theme": theme,
                                 "innerWidth": got, **summary})
                check(f"axe {width} {theme}", summary["counts"]["critical"] == 0
                      and summary["counts"]["serious"] == 0,
                      f"innerWidth={got} " + json.dumps(summary["counts"])
                      + ("" if not summary["violations"] else " " + json.dumps(summary["violations"][:3])))
                tab.screenshot(str(HERE / f"m118-value-{width}-{theme}.png"))

        # --- 9: console ------------------------------------------------------
        errors = [e for e in tab.console_errors if "favicon" not in e.lower()]
        check("console errors", not errors, json.dumps(errors[:3]) if errors else "0")

        report = {
            "base": args.base,
            "account": account,
            "url": url,
            "granularity": body.get("granularity"),
            "requested_granularity": body.get("requested_granularity"),
            "app_wire_capture": wire_seen is not None,
            "bucket_count": len(buckets),
            "event_count": len(events),
            "events": [
                {"date": e["date"], "kind": e["kind"], "amount_cad": e["amount_cad"],
                 "source": e["source"], "trade_id": e["trade_id"],
                 "transaction_id": e["transaction_id"]}
                for e in events
            ],
            "axe": axe_runs,
            "checks": CHECKS,
        }
        Path(args.out).write_text(json.dumps(report, indent=1))
        failed = [c for c in CHECKS if not c["ok"]]
        print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed"
              + (f" — FAILED: {[c['check'] for c in failed]}" if failed else ""),
              flush=True)
        return 1 if failed else 0
    finally:
        close_tab(target_id)
        # The minted access token has served its purpose (15-minute TTL
        # anyway) — nothing of it stays on disk.
        Path(args.token_file).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

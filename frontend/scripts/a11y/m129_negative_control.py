#!/usr/bin/env python3
"""Negative control for the m129 tick gate (t_bed5252d ask #1).

A deterministic wait is only worth having if a REAL missing badge still
fails. This runs the same tick sequence with the price stream gutted (the
EventSource is a no-op stub, so no cached-price patch can ever reach the
cache) and reports whether the probe's own predicate ever became true.

Expected: badges never appear and the tick verdict would be FAIL.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from a11y_driver import Browser  # noqa: E402
from m129_delta_probe import (  # noqa: E402
    GATE_SOURCE, PASSWORD, install_gate, overview_state, release_overview,
    seed, settle, ui_login, wait_stream_message,
)

STUB = """(() => {
      window.EventSource = function () {
        this.readyState = 2;
        this.withCredentials = false;
        this.addEventListener = () => {};
        this.removeEventListener = () => {};
        this.dispatchEvent = () => true;
        this.close = () => {};
        setTimeout(() => this.onerror && this.onerror(new Event('error')), 0);
      };
      window.EventSource.CONNECTING = 0;
      window.EventSource.OPEN = 1;
      window.EventSource.CLOSED = 2;
      return 'stubbed';
    })()"""


def main() -> int:
    base = "https://fintrac.princessandmark.com"
    from probe_target import require_target
    require_target(base, who="m129_negative_control.py")
    browser = Browser(width=390, height=844)
    out: dict = {}
    try:
        tab = browser.new_tab(f"{base}/login")
        tab.enable_runtime()
        tab.send("Page.enable")
        tab.send("Page.addScriptToEvaluateOnNewDocument", {"source": STUB})
        tab.send("Page.addScriptToEvaluateOnNewDocument", {"source": GATE_SOURCE})
        tab.navigate(f"{base}/login", timeout=45)
        settle(tab, 1.0)
        print("eventsource:", tab.evaluate(
            "typeof window.EventSource === 'function' && "
            "String(window.EventSource).includes('readyState = 2')"), flush=True)
        seeded = seed(tab, int(time.time()))
        sid = seeded["held_id"]
        ui_login(tab, base, seeded["email"])
        tab.navigate(f"{base}/securities/{sid}")
        settle(tab, 2.5)
        print("gate:", install_gate(tab), flush=True)
        rest = overview_state(tab)
        print(f"at rest: badges={rest['live_badges']} delta={rest['delta']}", flush=True)

        stream_waited = wait_stream_message(tab, timeout=10)
        tab.evaluate("window.__m129.hold = true")
        posted = tab.evaluate(
            f"""(async () => {{
                 const h = {{'Content-Type': 'application/json'}};
                 const log = await fetch('/api/v1/auth/login',
                   {{method:'POST', headers:h, body: JSON.stringify({{email: {json.dumps(seeded['email'])}, password: {json.dumps(PASSWORD)}}})}});
                 const auth = {{...h, Authorization: 'Bearer ' + (await log.json()).access_token}};
                 const day = new Date(Date.now() - 86400000).toISOString().slice(0,10);
                 const r = await fetch('/api/v1/prices', {{method:'POST', headers:auth,
                   body: JSON.stringify({{security_id: {sid}, date: day, price: '130'}})}});
                 return r.status;
               }})()""",
            await_promise=True,
        )
        print(f"POST /prices -> {posted}; stream-message wait: {stream_waited}", flush=True)

        deadline, samples, badge_seen = time.time() + 32, 0, False
        last = {}
        while time.time() < deadline:
            last = overview_state(tab)
            samples += 1
            if last["live_badges"] >= 1:
                badge_seen = True
                break
            tab.sleep(0.4)
        released = release_overview(tab)
        settle_state = overview_state(tab)
        out = {
            "stream_message_waited_s": stream_waited,
            "post_status": posted,
            "samples_taken": samples,
            "badge_seen": badge_seen,
            "tick_verdict_would_be": "PASS" if badge_seen else "FAIL",
            "final": {"badges": last.get("live_badges"),
                      "headline": (last.get("headline") or "")[:120],
                      "delta": last.get("delta")},
            "after_release": {"badges": settle_state.get("live_badges"),
                              "headline": (settle_state.get("headline") or "")[:120]},
            "released": released,
        }
    finally:
        (HERE / "m129-negative-control.json").write_text(json.dumps(out, indent=2))
        print(json.dumps(out, indent=2), flush=True)
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""How many page loads until the session dies? Records refresh-endpoint status per navigation."""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from a11y_driver import Browser  # noqa: E402

BASE = os.environ.get("A11Y_BASE", "http://localhost:4173")
from probe_target import require_target  # noqa: E402
require_target(BASE, who="session_probe.py")
PATHS = ["/dashboard", "/transactions", "/investments", "/taxes", "/budgets", "/bills", "/dashboard", "/transactions"]

b = Browser()
tab = b.new_tab(BASE + "/login")
try:
    tab.enable_runtime()
    tab.send("Page.enable")
    tab.send("Network.enable")
    responses: list[dict] = []
    tab.navigate(BASE + "/login", timeout=45)
    tab.wait_for(
        "Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Explore the demo')) !== undefined",
        timeout=25,
    )
    tab.sleep(0.5)
    tab.evaluate(
        "Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Explore the demo'))?.click()"
    )
    tab.wait_for("location.pathname === '/dashboard'", timeout=25)
    print("logged in at", time.strftime("%H:%M:%S"), flush=True)

    for i, path in enumerate(PATHS, 1):
        mark = len(responses)
        tab.navigate(BASE + path, timeout=45)
        tab.sleep(2.5)
        path_now = tab.evaluate("location.pathname")
        # drain network events
        for ev in tab.events("Network.responseReceived"):
            r = ev["params"]["response"]
            url = r["url"]
            if "refresh" in url or "login" in url or "demo" in url:
                responses.append({"url": url, "status": r["status"]})
        new = responses[mark:]
        print(f"  #{i} asked={path} landed={path_now} auth={json.dumps(new)}", flush=True)
        if path_now != path:
            print("  SESSION LOST", flush=True)
            break
finally:
    b.close()

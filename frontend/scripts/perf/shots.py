#!/usr/bin/env python3
"""M101 visual regression: shipped CSS vs the pre-M101 look.

`shipped`  = the built bundle exactly as it now is (blur killed below md).
`restored` = same page with the pre-M101 surface styles re-injected, i.e. the
             blur the media query removed plus the header's old /80 alpha.

At 412 the two must differ only by the frost (same tokens, same layout); at
1440 the media query does not apply, so the two must be pixel-identical —
that pair is the proof the desktop glass surface was left alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import websocket

BASE = os.environ.get("SHOTS_BASE", "http://127.0.0.1:4173")
SCRATCH = os.environ.get("TMPDIR", "/tmp")

RESTORE_CSS = """
/* Re-create the pre-M101 look on the shipped build: every blur the media
   query killed, at the utility's real radius (sm=4px, xl=24px), plus the
   header's old 80% alpha. Only used for screenshot A/B. */
.glass { backdrop-filter: blur(14px) !important;
         -webkit-backdrop-filter: blur(14px) !important;
         isolation: auto !important; }
.glass-strong { backdrop-filter: blur(18px) !important;
                -webkit-backdrop-filter: blur(18px) !important;
                isolation: auto !important; }
header.fixed.inset-x-0.top-0 {
  backdrop-filter: blur(24px) !important;
  -webkit-backdrop-filter: blur(24px) !important;
  background: rgb(var(--color-surface-raised) / 0.8) !important;
}
#app-nav { backdrop-filter: blur(24px) !important;
           -webkit-backdrop-filter: blur(24px) !important; }
[class*="backdrop-blur-sm"] { backdrop-filter: blur(4px) !important;
                              -webkit-backdrop-filter: blur(4px) !important; }
[class*="backdrop-blur-xl"]:not(header):not(#app-nav) {
  backdrop-filter: blur(24px) !important;
  -webkit-backdrop-filter: blur(24px) !important;
}
"""


def http_json(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode() or "null")


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, max_size=256 * 1024 * 1024, timeout=60)
        self._id = 0

    def cmd(self, method: str, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str):
        res = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True)
        if "exceptionDetails" in res:
            raise RuntimeError(json.dumps(res["exceptionDetails"])[:500])
        return res["result"].get("value")


def launch(vw: int, vh: int, dpr: float, mobile: bool):
    prof = os.path.join(SCRATCH, "m101-shot-prof")
    shutil.rmtree(prof, ignore_errors=True)
    cmd = [
        "chromium",
        "--remote-debugging-port=9455",
        f"--user-data-dir={prof}",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--hide-scrollbars",
        "--headless=new",
        "about:blank",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws_url = None
    for _ in range(100):
        try:
            for t in http_json("http://127.0.0.1:9455/json/list"):
                if t.get("type") == "page":
                    ws_url = t["webSocketDebuggerUrl"]
        except Exception:
            pass
        if ws_url:
            break
        time.sleep(0.2)
    if not ws_url:
        raise RuntimeError("no chrome target")
    cdp = CDP(ws_url)
    cdp.cmd("Page.enable")
    cdp.cmd("Runtime.enable")
    cdp.cmd(
        "Emulation.setDeviceMetricsOverride",
        width=vw,
        height=vh,
        deviceScaleFactor=dpr,
        mobile=mobile,
    )
    return proc, cdp


def main() -> int:
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    variants = (
        tuple(sys.argv[2].split(","))
        if len(sys.argv) > 2
        else (
            "shipped",
            "restored",
        )
    )
    themes = ("dark", "light")
    pages = ("/dashboard", "/transactions")
    jobs = [(412, 915, 2.6, True, "mobile"), (1440, 900, 1.0, False, "desktop")]

    proc, cdp = None, None
    try:
        for vw, vh, dpr, mobile, label in jobs:
            proc, cdp = launch(vw, vh, dpr, mobile)
            cdp.eval("window.scrollTo(0,0); true")
            cdp.cmd("Page.navigate", url=BASE + "/login")
            time.sleep(2.5)
            cdp.eval(
                "(() => { const b=[...document.querySelectorAll('button')]"
                ".find(x=>/explore the demo/i.test(x.textContent||''));"
                " if(b) b.click(); return 'ok'; })()"
            )
            time.sleep(3)
            for theme in themes:
                # index.html's bootstrap reads localStorage("theme") on every
                # load, so the choice has to be persisted BEFORE navigating —
                # a classList tweak alone is wiped by the next navigation.
                cdp.eval(
                    "localStorage.setItem('theme', " + json.dumps(theme) + ");"
                    " document.documentElement.classList."
                    + ("add" if theme == "dark" else "remove")
                    + "('dark'); true"
                )
                for page in pages:
                    cdp.cmd("Page.navigate", url=BASE + page)
                    time.sleep(3.5)
                    cdp.eval(
                        "document.documentElement.classList."
                        + ("add" if theme == "dark" else "remove")
                        + "('dark'); true"
                    )
                    cdp.eval("window.scrollTo(0,0); true")
                    time.sleep(0.6)
                    for variant in variants:
                        if variant == "restored":
                            cdp.eval(
                                "(() => { let s=document.getElementById('__restore');"
                                " if(!s){s=document.createElement('style');"
                                " s.id='__restore'; document.head.appendChild(s);}"
                                " s.textContent=" + json.dumps(RESTORE_CSS) + "; return true; })()"
                            )
                        else:
                            cdp.eval(
                                "(() => { const s=document.getElementById('__restore');"
                                " if(s) s.remove(); return true; })()"
                            )
                        time.sleep(0.8)
                        shot = cdp.cmd("Page.captureScreenshot", format="png")
                        name = f"{label}_{theme}_{page.strip('/')}_{variant}.png"
                        path = os.path.join(out_dir, name)
                        with open(path, "wb") as fh:
                            fh.write(__import__("base64").b64decode(shot["data"]))
                        print("wrote", path, flush=True)
            cdp.cmd("Browser.close")
            proc.wait(timeout=5)
            proc, cdp = None, None
    finally:
        if proc:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Minimal CDP driver for the M58 accessibility audit.

Launches headless Chrome with `--headless=old` (AGENTS §5), drives it over the
DevTools websocket with stdlib + websocket-client, and exposes the handful of
operations the audit needs: navigate, wait, evaluate, press keys, screenshot.

Usage:
    from a11y_driver import Browser
    with Browser() as b:
        page = b.new_page("http://localhost:4173/login")
        page.evaluate("document.title")
"""

from __future__ import annotations

import json
import os
import tempfile
import subprocess
import time
import urllib.request
from typing import cast

import websocket

CHROME = "/usr/bin/chromium"
DEBUG_PORT = int(os.environ.get("A11Y_CDP_PORT", "9455"))
USER_DATA_DIR = os.environ.get(
    "A11Y_PROFILE_DIR", os.path.join(tempfile.gettempdir(), "fintrack-a11y-profile")
)


class CDPSession:
    """One flat CDP session bound to a target (page)."""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=60, origin="http://127.0.0.1")
        self._id = 0
        self._pending: dict[int, dict] = {}

    def send(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        self._id += 1
        msg_id = self._id
        payload: dict = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self.ws.send(json.dumps(payload))
        while True:
            raw = self.ws.recv()
            if not isinstance(raw, str):
                continue
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})
            # Buffer async events per session+method so each tab drains its own.
            if "method" in data:
                key = f"{data.get('sessionId', 'browser')}|{data['method']}"
                self._pending.setdefault(key, []).append(data)

    def drain(self, key: str) -> list[dict]:
        return self._pending.pop(key, [])

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


class Browser:
    def __init__(self, headless_old: bool = True, width: int = 1440, height: int = 900):
        flags = [
            CHROME,
            "--headless=old" if headless_old else "--headless",
            f"--remote-debugging-port={DEBUG_PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={USER_DATA_DIR}",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={width},{height}",
            "--force-device-scale-factor=1",
            "--lang=en-CA",
            "about:blank",
        ]
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        extra = os.environ.get("A11Y_CHROME_FLAGS", "").split()
        # A11Y_CHROME_CWD: where --prof writes its isolate-*.log (debug only).
        cwd = os.environ.get("A11Y_CHROME_CWD") or None
        self.proc = subprocess.Popen(
            flags + extra, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=cwd
        )
        self.ws_url = self._wait_for_devtools()
        self.browser_cdpsession = CDPSession(self.ws_url)
        self.pages: list["Tab"] = []

    def _wait_for_devtools(self, timeout: float = 30.0) -> str:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=2) as r:
                    return json.load(r)["webSocketDebuggerUrl"]
            except Exception as exc:  # not up yet
                last = exc
                time.sleep(0.25)
        raise RuntimeError(f"DevTools never came up: {last}")

    def new_tab(self, url: str = "about:blank") -> "Tab":
        res = self.browser_cdpsession.send("Target.createTarget", {"url": url})
        tab = Tab(self, res["targetId"])
        self.pages.append(tab)
        return tab

    def close(self) -> None:
        for p in self.pages:
            p.close()
        self.browser_cdpsession.close()
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


class Tab:
    def __init__(self, browser: "Browser", target_id: str):
        self.browser = browser
        self.target_id = target_id
        attach = browser.browser_cdpsession.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        self.session_id = attach["sessionId"]
        self.conn = browser.browser_cdpsession

    # -- low level ------------------------------------------------------
    def send(self, method: str, params: dict | None = None) -> dict:
        return self.conn.send(method, params, session_id=self.session_id)

    def events(self, method: str) -> list[dict]:
        return self.conn.drain(f"{self.session_id}|{method}")

    # -- navigation / timing --------------------------------------------
    def navigate(self, url: str, timeout: float = 30.0) -> None:
        self.send("Page.enable")
        self.send("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self.evaluate("document.readyState")
                if state == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise TimeoutError(f"{url} never reached readyState=complete")

    def wait_for(self, expr: str, timeout: float = 20.0, poll: float = 0.25) -> object:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                val = self.evaluate(expr)
                if val:
                    return val
                last = val
            except Exception as exc:
                last = exc
            time.sleep(poll)
        raise TimeoutError(f"wait_for({expr}) timed out; last={last!r}")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    # -- viewport ---------------------------------------------------------
    def viewport(self) -> list[int]:
        """The viewport this document is ACTUALLY laid out at."""
        return cast(list[int], self.evaluate("[window.innerWidth, window.innerHeight]"))

    def force_viewport(self, width: int, height: int) -> list[int]:
        """Pin this target's viewport and return what the page actually got.

        `--window-size` is NOT the viewport: headless Chrome clamps a window
        below 500 CSS px, so `--window-size 360` lays the page out at 500 and
        the narrow-width rules (`scrollable-region-focusable` and friends) are
        never exercised — while a report that only reads the requested width
        claims 360. The emulation override is per-target and survives
        navigations, but a target created WITH a url can commit its first
        document before the first call lands, so re-apply this once a document
        is up (M102; the canonical fix lives here, `axe_scan.py` and
        `site_axe_scan.py` both drive it).
        """
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        return self.viewport()

    # -- script ----------------------------------------------------------
    def evaluate(self, expr: str, await_promise: bool = False) -> object:
        res = self.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if "exceptionDetails" in res:
            details = res["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text")
            raise RuntimeError(f"page exception: {text}")
        return res.get("result", {}).get("value")

    def console_errors(self) -> list[str]:
        msgs = self.events("Runtime.consoleAPICalled")
        out = []
        for m in msgs:
            if m["params"]["type"] in ("error", "warning"):
                args = " ".join(
                    str(a.get("value", a.get("description", ""))) for a in m["params"]["args"]
                )
                out.append(f"{m['params']['type']}: {args}")
        for m in self.events("Runtime.exceptionThrown"):
            d = m["params"]["exceptionDetails"]
            out.append("exception: " + str(d.get("exception", {}).get("description") or d.get("text")))
        # CSP refusals never reach consoleAPICalled/exceptionThrown: Chrome
        # reports them on the Log domain as `security-policy` entries. The
        # channel above alone reported "0 console errors" while Chrome was
        # blocking BOTH inline scripts in index.html on every load (QA F2 —
        # 2 per load, 72 across 36 loads), so the claim it produced was an
        # instrument artifact, not a measurement.
        # `source` is `security` (not `security-policy`) — confirmed against
        # the deployed stack: 2 entries with the old `script-src 'self'`, 0
        # with the hashes pinned. Scoped to security/violation so a network
        # 404 cannot silently change what "console errors" means here.
        for m in self.events("Log.entryAdded"):
            entry = m["params"]["entry"]
            if entry.get("level") == "error" and entry.get("source") in (
                "security",
                "violation",
            ):
                out.append(f"csp: {entry.get('text', '')}")
        return out

    def enable_runtime(self) -> None:
        self.send("Runtime.enable")
        self.send("Page.enable")
        # Required for the security-policy entries read by console_errors();
        # without it CSP violations are invisible to every harness here.
        self.send("Log.enable")

    # -- input -------------------------------------------------------------
    def press(self, key: str, modifiers: int = 0) -> None:
        """Press one key so Chrome runs its DEFAULT ACTION, not just an event.

        Two traps, both measured on this host's Chrome 154 against a plain
        file:// page (reproduce with `press_regression.py`, which prints the
        three-way table rather than taking this docstring's word for it):

        * `rawKeyDown` + `keyUp` with no `text` reaches the DOM as a keydown
          but runs **no default action**: a focused <button> does not click on
          Enter and an implicit form submission never fires. The keys that
          need that default action are dispatched as `keyDown` WITH `text`,
          which emits keydown and the char in ONE event.
        * Do NOT add an explicit `char` on top of `keyDown`+`text`: Chrome
          then runs the default action TWICE (measured: 2 clicks, 2 submits).
          That is the m106 helper's recipe and it double-fired.

        `Tab`, `Escape` and the arrows keep `rawKeyDown`: they have no text,
        their default action (focus traversal) runs off the keydown itself —
        measured moving focus BODY→BUTTON→A→INPUT→TEXTAREA across a fresh
        document, with and without a sleep before reading `activeElement`.

        `Space` also needs the right `key` VALUE: the DOM value is a single
        space (`" "`), not `"Space"` (that string is the `code`). With
        `key="Space"` Chrome matches nothing and the button never activates.
        """
        # CDP wants `code` as the DOM *string* ("Tab", "Escape", "ArrowDown").
        # Passing the numeric keyCode there fails to deserialize — the browser
        # rejects Input.dispatchKeyEvent with "string value expected".
        vk = KEY_CODES.get(key, 0) or ord(key[:1].upper())
        base = {
            "code": DOM_CODES.get(key, key),
            "key": KEY_VALUES.get(key, key),
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": modifiers,
        }
        text = key_text(key)
        if text:
            self.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": text, **base})
        else:
            self.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", **base})
        self.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    def type_text(self, text: str) -> None:
        """Type `text` into the focused element — ONCE per glyph.

        `keyDown` with `text` already emits the char event, so a separate
        `char` dispatch on top made every glyph land twice ("pprroobbee" —
        measured; m104 grew its own `type_once()` around this). Only the
        non-printable branch still needs an explicit `char`, because that one
        goes out as `rawKeyDown`, which carries no text of its own.
        """
        for ch in text:
            printable = ch.isprintable()
            self.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown" if printable else "rawKeyDown",
                    "text": ch,
                    "key": ch,
                    "code": f"Key{ch.upper()}" if ch.isalnum() else "",
                    "windowsVirtualKeyCode": ord(ch.upper()) if ch.isalnum() else 0,
                    "modifiers": 0,
                },
            )
            if not printable:
                self.send(
                    "Input.dispatchKeyEvent",
                    {"type": "char", "text": ch, "key": ch, "modifiers": 0},
                )
            self.send(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "key": ch, "modifiers": 0},
            )

    def click_at(self, x: float, y: float) -> None:
        for kind in ("mousePressed", "mouseReleased"):
            self.send(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
            )

    def screenshot(self, path: str, full_page: bool = False) -> str:
        params = {"format": "png"}
        if full_page:
            metrics = self.send("Page.getLayoutMetrics")
            size = metrics.get("cssContentSize") or metrics.get("contentSize")
            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": size["width"],
                "height": size["height"],
                "scale": 1,
            }
            params["captureBeyondViewport"] = True
        res = self.send("Page.captureScreenshot", params)
        import base64

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(res["data"]))
        return path

    def close(self) -> None:
        try:
            self.browser.browser_cdpsession.send("Target.closeTarget", {"targetId": self.target_id})
        except Exception:
            pass


KEY_CODES = {
    "Tab": 9,
    "Escape": 27,
    "Enter": 13,
    "Space": 32,
    "ArrowDown": 40,
    "ArrowUp": 38,
    "ArrowLeft": 37,
    "ArrowRight": 39,
    "Shift": 16,
}

# DOM `code` strings (physical key) for the same set — `code` is a string in
# Input.dispatchKeyEvent, never the numeric keyCode.
DOM_CODES = {
    "Tab": "Tab",
    "Escape": "Escape",
    "Enter": "Enter",
    "Space": "Space",
    "ArrowDown": "ArrowDown",
    "ArrowUp": "ArrowUp",
    "ArrowLeft": "ArrowLeft",
    "ArrowRight": "ArrowRight",
    "Shift": "ShiftLeft",
}

# The DOM KeyboardEvent.key VALUE where it differs from the name probes pass.
# Only Space differs: `key` is a single space (" "), while "Space" is the
# `code`. Sending key="Space" made Chrome match no key at all, so a focused
# button never activated on Space (measured — press_regression.py).
KEY_VALUES = {
    "Space": " ",
}

# Keys that carry text, and the text they carry. These are dispatched as
# `keyDown` WITH `text` so Chrome runs the default action (button activation,
# implicit form submission) exactly once. Everything else keeps `rawKeyDown`.
TEXT_KEYS = {
    "Enter": "\r",
    "Space": " ",
}


def key_text(key: str) -> str:
    """The text a `press(key)` keystroke inserts, or "" for a non-text key.

    Exported so `press_regression.py` can print the exact dispatch the driver
    chose next to the measured outcome for that dispatch.
    """
    return TEXT_KEYS.get(key, "")

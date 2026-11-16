#!/usr/bin/env python3
"""CSP console-error probe — one origin, one number.

`Tab.console_errors()` used to read only `Runtime.consoleAPICalled` +
`Runtime.exceptionThrown`, and Chrome reports CSP refusals on neither: it
emits them as `Log.entryAdded` entries with `source=security`. That is why
docs/09 carried "0 console errors" rows while Chrome was blocking BOTH
inline scripts in `index.html` on every load (QA F2: 2 per load, 72 across
36 loads). The driver now reads that domain too; this probe is the two-line
proof that the channel is live:

    python3 scripts/m72/csp_console_probe.py --base http://127.0.0.1:18080
    #   -> 2 entries, both "Executing inline script violates ... script-src"

    python3 scripts/m72/csp_console_probe.py --base http://127.0.0.1:4177
    #   -> 0 entries (hashes pinned)

Each origin gets a FRESH page load, because the events are drained per load.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

os.environ["A11Y_PROFILE_DIR"] = os.path.join(
    tempfile.gettempdir(), f"fintrack-csp-{os.getpid()}"
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "a11y"
    ),
)
from a11y_driver import Browser  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    ap.add_argument("--path", default="/login")
    ap.add_argument("--loads", type=int, default=3)
    args = ap.parse_args()

    browser = Browser()
    total = 0
    try:
        tab = browser.new_tab("about:blank")
        tab.enable_runtime()
        for i in range(args.loads):
            tab.navigate(f"{args.base}{args.path}", timeout=30)
            tab.sleep(2.5)
            errors = tab.console_errors()
            total += len(errors)
            print(f"load {i + 1}: {len(errors)} console errors")
            for err in errors[:4]:
                print("    ", err[:160])
    finally:
        browser.close()
    print(f"total console errors across {args.loads} loads: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())

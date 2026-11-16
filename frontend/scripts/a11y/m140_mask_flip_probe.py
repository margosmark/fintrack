"""M140 evidence probe — is the mask flip REAL-TIME on the DEPLOYED stack?

M139's probe proved the chip works and the content masks. It did NOT prove the
routed pages re-render on a flip, which is exactly what Mark reported live:

    "hitting the mask/unmask does not happen in real time, needs a page refresh"

So this probe asks the one question M139 left open, three times — once each on
the surfaces he named. On EACH of `/investments`, `/dashboard`, `/accounts`:

  1. FLIP     — with the page already settled, click the mask chip.
  2. CONTENT  — do the dollar figures on that page become bullets, with NO
                navigation and NO reload? Asserted by diffing the page's money
                callouts across the click, and by requiring the URL to be
                unchanged (a reload would satisfy the diff for the wrong
                reason — restore-on-load is OFF, so a reload would come back
                from the tenant default instead).
  3. NETWORK  — ZERO requests during the flip. The mask lives below the data
                layer (docs/46 §D5/D6): flipping it must not invalidate a
                query, refetch, or re-probe the session. This is counted off
                the CDP Network domain, so it is Chrome's own view, not the
                page's self-report.
  4. ROUNDTRIP— unmask again: the dollars come back on the SAME page, still
                with zero requests. One-way masking could be a rendering bug;
                the round trip is what proves the re-render.

The demo tenant boots MASKED (docs/46 §D1), so every assertion is RELATIVE to
the observed boot state, never absolute — asserting "became true" here would
report a working mask as broken.

Run:
    A11Y_BASE=http://192.0.2.110:8080 A11Y_CDP_PORT=9481 \
    A11Y_PROFILE_DIR=/tmp/m140flippobe \
    python3 scripts/a11y/m140_mask_flip_probe.py --out m140-mask-flip-probe.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from a11y_driver import Browser

BASE = os.environ.get("A11Y_BASE", "http://127.0.0.1:8080")

BULLETS = "•••••"
MASK_KEY = "fintrack.mask"

# The three surfaces Mark named. Each has money that M139 got right (shell
# chrome) and money that M140 had to fix (the routed page).
SURFACES = ["/investments", "/dashboard", "/accounts"]

# One pass over the page: the leaf-node text of every standalone dollar figure
# that a USER CAN SEE, plus the URL, so a flip can be diffed against it. Money
# is an exact standalone figure — the same rule M139's probe used.
#
# Visible means visible: `aria-hidden` subtrees and off-screen nodes are
# excluded. This matters because Recharts appends `#recharts_measurement_span`
# straight to `<body>` to measure tick text width — `aria-hidden`, parked at
# `top: -20000px`, never rendered. It still holds a raw `$80K` while masked
# (it is a scratchpad, not a figure), and counting it made a correct mask look
# like a leak. What the mask owes the user is the SCREEN.
SNAPSHOT_JS = """
(() => {
  const moneyRe = /^-?\$[\d,]+(?:\.\d{2})?[KMB]?$/;
  const money = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 0) continue;
    if (el.closest('[aria-hidden="true"]')) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    if (rect.bottom < 0 || rect.top > (window.innerHeight || 0) + 40) continue;
    const t = (el.textContent || '').trim();
    if (moneyRe.test(t)) money.push(t);
  }
  return { money, path: location.pathname, title: document.title };
})()
"""

CHIP_JS = """
(() => {
  const chip = document.querySelector('[data-testid="mask-chip"]');
  return chip ? chip.getAttribute('aria-pressed') : null;
})()
"""


def wait_stable(tab, timeout: float = 12.0) -> None:
    """Wait until the DOM stops churning — these pages land data late."""
    deadline = time.time() + timeout
    last = -1
    quiet = 0
    while time.time() < deadline:
        try:
            count = tab.evaluate("document.querySelectorAll('*').length")
        except Exception:
            time.sleep(0.25)
            continue
        if count == last:
            quiet += 1
            if quiet >= 3:
                return
        else:
            quiet = 0
        last = count
        time.sleep(0.35)


def open_session(browser: Browser, width: int, height: int):
    """Log into the demo tenant exactly as the house probes do."""
    tab = browser.new_tab(f"{BASE}/login")
    tab.enable_runtime()
    tab.force_viewport(width, height)
    # Chrome's own view of the network — the flip's zero-request claim is
    # counted here, not self-reported by the page.
    tab.send("Network.enable")
    tab.navigate(f"{BASE}/login")
    wait_stable(tab)
    tab.evaluate(
        "Array.from(document.querySelectorAll('button')).find("
        "b => b.textContent.includes('Explore the demo'))?.click()"
    )
    tab.wait_for("location.pathname === '/dashboard'", timeout=25)
    wait_stable(tab)
    return tab


def set_masked(tab, want: bool, timeout: float = 10.0) -> bool:
    """Drive the chip to a target state, RELATIVE to wherever it is now."""
    if (tab.evaluate(CHIP_JS) == "true") == want:
        return True
    tab.evaluate("document.querySelector('[data-testid=\"mask-chip\"]')?.click()")
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"mask-chip\"]')"
            f"?.getAttribute('aria-pressed') === '{str(want).lower()}'",
            timeout=timeout,
        )
    except Exception:
        return False
    tab.sleep(0.5)
    return True


def snapshot(tab) -> dict:
    return tab.evaluate(SNAPSHOT_JS) or {"money": [], "path": None, "title": None}


def drain_requests(tab) -> list[str]:
    """Every network request Chrome saw since the last drain."""
    urls = []
    for ev in tab.events("Network.requestWillBeSent"):
        url = (ev.get("params", {}).get("request", {}) or {}).get("url", "")
        if url:
            urls.append(url)
    return urls


def spa_goto(tab, path: str, timeout: float = 20.0) -> bool:
    """Route to `path` WITHOUT a page load — by clicking the shell's own nav.

    A hard `tab.navigate()` here loses the session: the demo tenant's login
    issues no refresh cookie (`POST /auth/refresh` answers 401, measured), and
    the access token lives in memory only. So the probe must move the way a
    user does — an in-app link click, which is also the path M140 is about
    (the mask has to survive SPA navigation, docs/46 §D1).
    """
    tab.evaluate(
        """
        (() => {
          const want = %s;
          const a = [...document.querySelectorAll('a[href]')]
            .find(x => (x.getAttribute('href') || '') === want);
          if (a) { a.click(); return true; }
          // No rail entry for this path (e.g. /accounts on the narrow rail):
          // push the route and let the router pick it up on the next popstate.
          history.pushState({}, '', want);
          window.dispatchEvent(new PopStateEvent('popstate'));
          return false;
        })()
        """
        % json.dumps(path)
    )
    try:
        tab.wait_for(f"location.pathname === {json.dumps(path)}", timeout=timeout)
    except Exception:
        return False
    return True


def probe_surface(browser: Browser, path: str, width: int, height: int) -> dict:
    """One surface, one settled page, one flip, one unflip."""
    out: dict = {"path": path}
    tab = open_session(browser, width, height)
    if not spa_goto(tab, path):
        out["error"] = f"could not SPA-route to {path} (landed on {tab.evaluate('location.pathname')})"
        return out
    wait_stable(tab, timeout=18)
    # Let late queries land so the settled page is the thing we flip.
    tab.sleep(1.5)

    boot = tab.evaluate(CHIP_JS)
    boot_masked = boot == "true"
    out["bootMasked"] = boot_masked

    # Drive to a KNOWN baseline rather than trusting the boot state. The demo
    # tenant boots MASKED (docs/46 §D1), so its boot state has no dollar figures
    # to hide — asserting against it would prove nothing. Unmasking first gives
    # a real, non-empty money set to flip AWAY from, which is the stronger
    # direction anyway: it is the one where a broken re-render would leave real
    # dollars on a masked screen.
    set_masked(tab, False)
    tab.sleep(0.8)
    before = snapshot(tab)
    out["before"] = {
        "moneyCount": len(before["money"]),
        "moneySample": before["money"][:8],
        "path": before["path"],
    }

    # The whole probe is meaningless if the page shows no money at all — that
    # would mean the regex, the route, or the login silently broke.
    if not before["money"]:
        out["error"] = "no money callouts on the page at all — nothing to flip"
        return out

    # THE flip: unmasked → masked, in place.
    drain_requests(tab)  # discard whatever the settle produced
    set_masked(tab, True)
    tab.sleep(1.0)
    after = snapshot(tab)
    flip_requests = drain_requests(tab)

    out["afterFlip"] = {
        "moneyCount": len(after["money"]),
        "bulletCount": json.dumps(after["money"]).count(BULLETS),
        "moneySample": after["money"][:8],
        "path": after["path"],
    }
    out["flip"] = {
        # The page re-rendered: the callout set actually changed.
        "changed": before["money"] != after["money"],
        # …and it did so WITHOUT navigation. A reload would also change the
        # money (restore-on-load is OFF, so it would come back from the tenant
        # DEFAULT), so this is what separates a real re-render from a reload.
        "noNavigation": before["path"] == after["path"] == path,
        "noRequests": len(flip_requests) == 0,
        "requestUrls": flip_requests[:10],
        # The money is GONE, not merely different.
        "dollarsGone": len(after["money"]) == 0,
    }

    # Round trip back to revealed, still in place, still silent: the original
    # figures return byte-for-byte.
    drain_requests(tab)
    set_masked(tab, False)
    tab.sleep(1.0)
    back = snapshot(tab)
    back_requests = drain_requests(tab)
    out["roundTrip"] = {
        "moneyCount": len(back["money"]),
        "restored": back["money"] == before["money"],
        "noNavigation": back["path"] == path,
        "noRequests": len(back_requests) == 0,
        "requestUrls": back_requests[:10],
    }

    out["consoleErrors"] = [e for e in tab.console_errors() if "favicon" not in e][:5]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="m140-mask-flip-probe.json")
    ap.add_argument("--widths", default="1440,390")
    args = ap.parse_args()
    from probe_target import require_target
    require_target(BASE, who="m140_mask_flip_probe.py")

    widths = [int(w) for w in args.widths.split(",") if w.strip()]
    report: dict = {"base": BASE, "surfaces": {}}

    browser = Browser(width=1440, height=900)
    try:
        for w in widths:
            h = 900 if w > 500 else 844
            for path in SURFACES:
                key = f"{w}{path}"
                try:
                    report["surfaces"][key] = probe_surface(browser, path, w, h)
                except Exception as exc:  # noqa: BLE001 — the probe reports, it doesn't crash
                    report["surfaces"][key] = {"path": path, "width": w, "error": repr(exc)}
    finally:
        browser.close()

    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {out_path}", file=sys.stderr)

    # Verdict.
    failures: list[str] = []
    for key, data in report["surfaces"].items():
        if "error" in data:
            failures.append(f"{key}: probe error {data['error']}")
            continue
        flip = data.get("flip", {})
        if not flip.get("changed"):
            failures.append(f"{key}: the money callouts did NOT change on a flip")
        if not flip.get("noNavigation"):
            failures.append(f"{key}: the page navigated during the flip")
        if not flip.get("noRequests"):
            failures.append(
                f"{key}: {len(flip.get('requestUrls', []))} request(s) fired during the flip"
            )
        if not flip.get("dollarsGone"):
            failures.append(f"{key}: masking left a dollar figure on screen")
        rt = data.get("roundTrip", {})
        if not rt.get("restored"):
            failures.append(f"{key}: unmasking did not restore the original figures")
        if not rt.get("noNavigation"):
            failures.append(f"{key}: the page navigated during the UNMASK")
        if not rt.get("noRequests"):
            failures.append(f"{key}: requests fired during the UNMASK")

    if failures:
        print("\nM140 PROBE FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("\nM140 PROBE OK: every surface flips in place, instantly, with zero requests.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

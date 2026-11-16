"""M139 evidence probe — does ⌘⇧H actually mask the money, in the DEPLOYED stack?

The unit tests prove the formatters; this proves the wiring that only exists in
a real browser against the built bundle behind nginx: the shell's one keydown
listener, the store subscription, and that the routed page re-renders from it.

CRITICAL (the bug the first cut of this probe had): the DEMO tenant boots
MASKED by default (docs/46 §D1, `applyDemoMaskDefault`). There is therefore no
fixed "unmasked baseline" to assume. Everything below is written RELATIVE to
the observed boot state — `boot_masked` is read first and flips are asserted as
"did it change", never "did it become true". Asserting an absolute state here
reports a working mask as broken.

Five questions, two viewport widths:

  1. CHIP     — is the chip a real <button> with aria-pressed + an icon, and
                does clicking it change the state?
  2. ⌘⇧H      — does the chord change it too, and do plain ⌘H and bare ⌘K stay
                quiet (⌘K belongs to the M95 palette)?
  3. CONTENT  — while masked, do dollar figures disappear, while PERCENTAGES
                stay readable (the operator's ruling) and dates, symbols and
                account names survive? Unmasking must bring the dollars back.
  4. SESSION  — does the choice survive a reload in the SAME tab, and does a
                FRESH tab fall back to the tenant default rather than
                inheriting the last tab's choice (sessionStorage, not
                localStorage)?
  5. CHROME   — polite live region announcing each flip.

Run:
    A11Y_BASE=http://192.0.2.109:8080 A11Y_CDP_PORT=9481 \
    A11Y_PROFILE_DIR=/tmp/m139maskprobe \
    python3 scripts/a11y/m139_mask_probe.py --out m139-mask-probe.json
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

# The bullets the mask renders, and the sessionStorage key it persists under.
BULLETS = "•••••"
MASK_KEY = "fintrack.mask"

# One page dump, three buckets. A percentage often sits INSIDE a cell that also
# carries an arrow or a CAD delta ("▲ +$1,633.38 (+0.72%)"), so percentage
# detection matches a % TOKEN anywhere in the leaf's text; money stays an exact
# standalone figure, which is what the mask must remove.
DUMP_JS = """
(() => {
  const moneyRe = /^-?\\$[\\d,]+(?:\\.\\d{2})?$/;
  const pctRe = /-?[\\d,]+(?:\\.\\d+)?%/;
  const money = [], pcts = [], words = [];
  for (const el of document.querySelectorAll("body *")) {
    if (el.children.length > 0) continue;
    const t = (el.textContent || "").trim();
    if (!t) continue;
    if (moneyRe.test(t)) money.push(t);
    else if (pctRe.test(t)) pcts.push(t);
    else if (t.length > 3 && t.length < 90) words.push(t);
  }
  return { money, pcts, words };
})()
"""

CHIP_JS = """
(() => {
  const chip = document.querySelector('[data-testid="mask-chip"]');
  if (!chip) return null;
  return {
    pressed: chip.getAttribute('aria-pressed'),
    text: (chip.textContent || '').trim(),
    tag: chip.tagName,
    // three channels: icon (svg), text, aria-pressed — never colour alone
    hasSvg: !!chip.querySelector('svg'),
    rowPressed: document.querySelector('[data-testid="mask-toggle-row"]')
      ?.getAttribute('aria-pressed') ?? null,
  };
})()
"""


# --------------------------------------------------------------------------
# page helpers
# --------------------------------------------------------------------------
def wait_stable(tab, timeout: float = 12.0) -> None:
    """Wait until the DOM stops churning — pages here land data late."""
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
    """Log into the demo tenant the same way the house probes do."""
    tab = browser.new_tab(f"{BASE}/login")
    tab.enable_runtime()
    tab.force_viewport(width, height)
    tab.navigate(f"{BASE}/login")
    wait_stable(tab)
    tab.evaluate(
        "Array.from(document.querySelectorAll('button')).find("
        "b => b.textContent.includes('Explore the demo'))?.click()"
    )
    tab.wait_for("location.pathname === '/dashboard'", timeout=25)
    wait_stable(tab)
    return tab


def chip_state(tab) -> dict | None:
    return tab.evaluate(CHIP_JS)


def dump(tab) -> dict:
    return tab.evaluate(DUMP_JS) or {"money": [], "pcts": [], "words": []}


def set_masked(tab, want: bool, timeout: float = 10.0) -> bool:
    """Drive the chip to a target state, RELATIVE to wherever it is now."""
    if ((chip_state(tab) or {}).get("pressed") == "true") == want:
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
    tab.sleep(0.4)
    return True


def press_chord(tab, key: str = "h", meta: bool = True, shift: bool = True) -> None:
    """Dispatch the chord the way a user would — real modifiers on keydown."""
    mods = []
    if meta:
        mods.append("metaKey")
    if shift:
        mods.append("shiftKey")
    mod_js = ", ".join(f"{m}: true" for m in mods)
    tab.evaluate(
        f"""
        (() => {{
          window.dispatchEvent(new KeyboardEvent('keydown', {{
            key: '{key}', code: 'Key{key.upper()}',
            bubbles: true, cancelable: true{',' if mod_js else ''} {mod_js}
          }}));
        }})()
        """
    )


def live_region(tab) -> dict | None:
    return tab.evaluate(
        """
        (() => {
          const r = document.querySelector('[data-testid="mask-live-region"]');
          return r ? { text: (r.textContent||'').trim(),
                       live: r.getAttribute('aria-live') } : null;
        })()
        """
    )


def joined(items: list[str]) -> str:
    return " ".join(items)


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------
def probe_width(browser: Browser, width: int, height: int) -> dict:
    out: dict = {"width": width, "height": height}
    tab = open_session(browser, width, height)

    boot = chip_state(tab)
    boot_masked = (boot or {}).get("pressed") == "true"
    out["boot"] = {
        "chip": boot,
        "bootMasked": boot_masked,
        "sessionStorageKey": tab.evaluate(f"sessionStorage.getItem('{MASK_KEY}')"),
    }

    # ---- 1: the chip is a real button, and the menu row agrees -------------
    out["chipIsButton"] = bool(boot and boot["tag"] == "BUTTON" and boot["hasSvg"])
    out["rowInSync"] = boot is not None and boot.get("pressed") == boot.get("rowPressed")

    # ---- 3: CONTENT while masked (the demo boots masked, so read it first) --
    masked = dump(tab)
    out["maskedContent"] = {
        "moneySample": masked["money"][:8],
        "moneyCount": len(masked["money"]),
        "pctSample": masked["pcts"][:8],
        "pctCount": len(masked["pcts"]),
        "wordSample": masked["words"][:10],
        "liveRegion": live_region(tab),
    }

    # ---- 1 + 2: CLICK flip, asserted relative to boot ---------------------
    before_click = chip_state(tab)
    settled = set_masked(tab, not boot_masked)
    after_click = chip_state(tab)
    out["clickFlip"] = {
        "before": before_click,
        "after": after_click,
        "changed": (before_click or {}).get("pressed") != (after_click or {}).get("pressed"),
        "settled": settled,
    }

    revealed = dump(tab)
    out["revealedContent"] = {
        "moneySample": revealed["money"][:8],
        "moneyCount": len(revealed["money"]),
        "pctSample": revealed["pcts"][:8],
        "pctCount": len(revealed["pcts"]),
    }

    # While masked: no standalone dollar figures at all, percentages intact.
    out["contentVerdict"] = {
        "noDollarFiguresWhileMasked": len(masked["money"]) == 0,
        "percentagesVisibleWhileMasked": len(masked["pcts"]) > 0,
        "percentagesNotMasked": BULLETS not in joined(masked["pcts"]),
        "datesAndNamesSurvive": len(masked["words"]) > 3,
        # Unmasking must actually reveal — otherwise the bullets above would
        # just be "the page is broken", not "the mask works".
        "dollarsRevealedOnUnmask": len(revealed["money"]) > 0,
        "noBulletsWhenRevealed": BULLETS not in joined(revealed["money"]),
    }

    # ---- 2: the chord flips; plain ⌘H and ⌘K do not ------------------------
    before_chord = (chip_state(tab) or {}).get("pressed")
    press_chord(tab)
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"mask-chip\"]')"
            f"?.getAttribute('aria-pressed') !== '{before_chord}'",
            timeout=8,
        )
    except Exception:
        pass
    tab.sleep(0.4)
    after_chord = (chip_state(tab) or {}).get("pressed")
    chorded = dump(tab)
    out["chordFlip"] = {
        "stateBefore": before_chord,
        "stateAfter": after_chord,
        "changed": before_chord != after_chord,
        "moneyCountAfter": len(chorded["money"]),
        "liveRegionAfter": live_region(tab),
    }

    plain_before = (chip_state(tab) or {}).get("pressed")
    press_chord(tab, meta=True, shift=False)          # plain ⌘H
    tab.sleep(0.5)
    plain_after = (chip_state(tab) or {}).get("pressed")
    press_chord(tab, key="k", meta=True, shift=False)  # ⌘K = the palette (M95)
    tab.sleep(0.5)
    palette_open = bool(tab.evaluate(
        "!!document.querySelector('[role=\"dialog\"]')"
    ))
    out["chordDiscrimination"] = {
        "plainMetaHBefore": plain_before,
        "plainMetaHAfter": plain_after,
        "plainMetaHHarmless": plain_before == plain_after,
        "metaKDidNotMask": (chip_state(tab) or {}).get("pressed") == plain_before,
        "metaKOpenedPalette": palette_open,
    }

    # ---- 4: session persistence --------------------------------------------
    # Drive to MASKED deliberately so the question is unambiguous.
    set_masked(tab, True)
    out["persistence"] = {
        "keyValue": tab.evaluate(f"sessionStorage.getItem('{MASK_KEY}')"),
        "localStorageClean": tab.evaluate(
            f"window.localStorage.getItem('{MASK_KEY}') === null"
        ),
    }

    # The property sessionStorage exists for: an SPA route change must NOT
    # reset the mask (no remount flicker). Note this is deliberately NOT a
    # hard reload — restore-on-load is OFF by design (docs/46 §D1), so a
    # reload is expected to come back from the tenant default.
    tab.evaluate(
        "(() => { const a = [...document.querySelectorAll('a')]"
        ".find(x => (x.getAttribute('href') || '').toLowerCase().includes('invest'));"
        "if (a) a.click(); })()"
    )
    try:
        tab.wait_for("location.pathname.startsWith('/investments')", timeout=15)
    except Exception:
        pass
    tab.sleep(1.2)
    out["persistence"]["survivesSpaNavigation"] = (
        (chip_state(tab) or {}).get("pressed") == "true"
    )
    out["persistence"]["pathAfterNav"] = tab.evaluate("location.pathname")

    return out


def fresh_tab_check(browser: Browser, width: int, height: int) -> dict:
    """A NEW tab shares localStorage but not sessionStorage.

    "Boots masked" proves nothing on its own — that IS the demo default. The
    discriminating test is the opposite direction: leave the FIRST tab
    UNMASKED, then open a fresh tab. If restore-on-load leaked, the second tab
    would come up unmasked too; because the mask is session-scoped it must fall
    back to the tenant DEFAULT (masked, for the demo).
    """
    first = open_session(browser, width, height)
    set_masked(first, False)  # leave the first tab explicitly UNMASKED

    second = open_session(browser, width, height)
    return {
        "firstTabLeft": (chip_state(first) or {}).get("pressed"),
        "firstTabKey": first.evaluate(f"sessionStorage.getItem('{MASK_KEY}')"),
        "freshTabChip": chip_state(second),
        "freshTabKey": second.evaluate(f"sessionStorage.getItem('{MASK_KEY}')"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="m139-mask-probe.json")
    ap.add_argument("--widths", default="1440,390")
    args = ap.parse_args()
    from probe_target import require_target
    require_target(BASE, who="m139_mask_probe.py")

    widths = [int(w) for w in args.widths.split(",") if w.strip()]
    report: dict = {"base": BASE, "widths": {}}

    # Browser owns a real Chrome subprocess, so it is try/finally, not `with`
    # (matching the other probes — the driver is not a context manager).
    browser = Browser(width=1440, height=900)
    try:
        for w in widths:
            h = 900 if w > 500 else 844
            try:
                report["widths"][str(w)] = probe_width(browser, w, h)
            except Exception as exc:  # noqa: BLE001 — the probe reports, it doesn't crash
                report["widths"][str(w)] = {"error": repr(exc)}
        try:
            report["freshTab"] = fresh_tab_check(browser, 1440, 900)
        except Exception as exc:  # noqa: BLE001
            report["freshTab"] = {"error": repr(exc)}
    finally:
        browser.close()

    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {out_path}", file=sys.stderr)

    # Verdict: fail loudly if the wiring is broken at any width.
    failures = []
    for w, data in report["widths"].items():
        if "error" in data:
            failures.append(f"{w}: probe error")
            continue
        if not data.get("chipIsButton"):
            failures.append(f"{w}: chip is not a real <button> with an icon")
        if not data.get("rowInSync"):
            failures.append(f"{w}: account-menu row disagrees with the chip")
        if not data.get("clickFlip", {}).get("changed"):
            failures.append(f"{w}: click did not change the chip")
        cv = data.get("contentVerdict", {})
        if not cv.get("noDollarFiguresWhileMasked"):
            failures.append(f"{w}: dollar figures still on screen while masked")
        if not cv.get("percentagesVisibleWhileMasked"):
            failures.append(f"{w}: no percentage survived (operator ruling broken)")
        if not cv.get("percentagesNotMasked"):
            failures.append(f"{w}: a percentage was masked")
        if not cv.get("dollarsRevealedOnUnmask"):
            failures.append(f"{w}: unmasking did not bring the dollars back")
        if not data.get("chordFlip", {}).get("changed"):
            failures.append(f"{w}: ⌘⇧H did not change the mask")
        disc = data.get("chordDiscrimination", {})
        if not disc.get("plainMetaHHarmless"):
            failures.append(f"{w}: plain ⌘H flipped the mask")
        if not disc.get("metaKDidNotMask"):
            failures.append(f"{w}: ⌘K flipped the mask")
        if not data.get("persistence", {}).get("localStorageClean", False):
            failures.append(f"{w}: mask leaked into localStorage")
        if not data.get("persistence", {}).get("survivesSpaNavigation"):
            failures.append(f"{w}: mask did not survive an SPA route change")

    fresh = report.get("freshTab", {})
    if "error" not in fresh:
        # The discriminating pair: first tab UNMASKED, fresh tab back at the
        # DEFAULT. Anything else means the choice crossed the tab boundary.
        if fresh.get("firstTabLeft") == "false" and (
            (fresh.get("freshTabChip") or {}).get("pressed") != "true"
        ):
            failures.append("fresh tab inherited the prior tab's mask (restore-on-load)")
        if fresh.get("firstTabLeft") not in ("true", "false"):
            failures.append("fresh-tab test could not drive the first tab")

    if failures:
        print("\nPROBE FAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\nprobe OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

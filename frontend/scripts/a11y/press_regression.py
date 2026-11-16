"""Regression harness for `a11y_driver.press()` — the three-way measurement.

    python3 press_regression.py [--keep]   # exit 0 only if every check passes

WHY THIS EXISTS (and why it prints the table instead of trusting a docstring):

`press()` used to send `rawKeyDown` + `keyUp` with `text: ""`. On this host's
Chrome that reaches the DOM as a keydown but runs **no default action** — a
focused <button> did not click on Enter, and an implicit form submission never
fired. Every probe asserting keyboard behaviour through `press()` was therefore
asserting nothing, and a weak assertion would have reported a false PASS. The
m106 probe grew its own `press_enter()` around the bug; this file is what proves
the bug is fixed and keeps it fixed.

The table below is the measurement the bug report describes, run on a `file://`
page so no stack, login or network is involved:

    dispatch                                     Enter-on-button   form-submit
    -------------------------------------------  ----------------  -----------
    rawKeyDown + keyUp (the OLD press)           0                 0
    keyDown, no text                             0                 0
    keyDown + text + explicit char  (m106)       2  <- double      2 <- double
    keyDown + text (the NEW press)               1                 1
    element.click()  (the element is fine)       1                 0

The last row is 0 for the form on purpose: the counter-button sits outside the
<form>, so clicking it never submits. It is the control that proves the element
itself was never at fault.

Tab is called out separately because the original bug report claimed focus
never moved. It does move, and has all along: `rawKeyDown` runs focus traversal
off the keydown itself. The claim is re-measured here every run so the next
person sees the answer rather than inheriting either version of the story.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from a11y_driver import Browser, key_text  # noqa: E402

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>press regression</title>
<button id="btn">Go</button>
<a id="lnk" href="#x">link</a>
<input id="txt">
<div id="scroller" tabindex="0" style="width:100px;overflow-x:scroll">
  <div style="width:2000px;height:20px">wide</div>
</div>
<form id="frm"><input id="field" type="text"><button type="submit">Send</button></form>
<div id="dialog" role="dialog" aria-modal="true" hidden>
  <button id="inner">Inside</button>
</div>
<script>
window.__clicks = 0;
window.__subs = 0;
window.__keys = [];
document.getElementById('btn').addEventListener('click', () => { window.__clicks++; });
document.getElementById('frm').addEventListener('submit', (e) => {
  e.preventDefault(); window.__subs++;
});
document.addEventListener('keydown', (e) => window.__keys.push(e.key));
</script>
"""

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


def raw(tab, type_: str, **kw) -> None:
    tab.send("Input.dispatchKeyEvent", {"type": type_, **kw})


# ---------------------------------------------------------------- dispatch recipes

def measure_recipes(tab, uri: str) -> list[dict]:
    """The three-way table, one row per dispatch recipe, Enter and Space each."""
    rows: list[dict] = []

    def run(label: str, dispatch) -> None:
        for target, counter in (("button", "window.__clicks"),
                                ("form", "window.__subs")):
            tab.navigate(uri)
            tab.sleep(0.2)
            tab.evaluate(f"{counter} = 0")
            focus = ("document.getElementById('btn').focus()" if target == "button"
                     else "document.getElementById('field').focus()")
            tab.evaluate(focus)
            dispatch(tab, target)
            tab.sleep(0.2)
            rows.append({
                "recipe": label,
                "target": target,
                "activations": tab.evaluate(counter),
            })

    def dispatch_raw(tab, _target):
        base = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13, "modifiers": 0}
        raw(tab, "rawKeyDown", **base)
        raw(tab, "keyUp", **base)

    def dispatch_keydown_no_text(tab, _target):
        base = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13, "modifiers": 0}
        raw(tab, "keyDown", **base)
        raw(tab, "keyUp", **base)

    def dispatch_keydown_text_char(tab, _target):
        base = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13, "modifiers": 0}
        raw(tab, "keyDown", text="\r", **base)
        raw(tab, "char", text="\r", **base)
        raw(tab, "keyUp", **base)

    def dispatch_new_press(tab, _target):
        tab.press("Enter")

    def dispatch_click(tab, _target):
        tab.evaluate("document.getElementById('btn').click()")

    run("rawKeyDown+keyUp (OLD press)", dispatch_raw)
    run("keyDown, no text", dispatch_keydown_no_text)
    run("keyDown+text+explicit char (m106 helper)", dispatch_keydown_text_char)
    run("press('Enter') (driver now)", dispatch_new_press)
    run("element.click()", dispatch_click)
    return rows


# ------------------------------------------------------------------------ checks
def run(keep: bool) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="press-regression-"))
    uri = (tmp / "page.html")
    uri.write_text(PAGE, encoding="utf-8")
    uri = uri.as_uri()

    b = Browser()
    try:
        tab = b.new_tab(uri)
        tab.sleep(0.4)

        print("=== three-way measurement (file:// page, one row per recipe) ===")
        rows = measure_recipes(tab, uri)
        width = max(len(r["recipe"]) for r in rows)
        print(f"  {'recipe'.ljust(width)}  {'target':7s}  activations")
        for r in rows:
            print(f"  {r['recipe'].ljust(width)}  {r['target']:7s}  {r['activations']}")
        print()

        def act(recipe: str, target: str) -> int:
            return next(r["activations"] for r in rows
                        if r["recipe"] == recipe and r["target"] == target)

        old = "rawKeyDown+keyUp (OLD press)"
        ntext = "keyDown, no text"
        dbl = "keyDown+text+explicit char (m106 helper)"
        new = "press('Enter') (driver now)"
        click = "element.click()"

        # The premise of this card, restated as a live measurement: the old
        # dispatch really did nothing, and the element itself was never at fault.
        check("OLD rawKeyDown runs no default action (Enter/button)",
              act(old, "button") == 0, f"activations={act(old, 'button')}")
        check("OLD rawKeyDown runs no default action (Enter/form)",
              act(old, "form") == 0, f"activations={act(old, 'form')}")
        check("keyDown without text is equally inert",
              act(ntext, "button") == 0 and act(ntext, "form") == 0,
              f"button={act(ntext, 'button')} form={act(ntext, 'form')}")
        check("the element itself was never broken (click works)",
              act(click, "button") == 1, f"activations={act(click, 'button')}")

        # The double-fire the m106 helper was carrying — documented so nobody
        # "fixes" press() back into it.
        check("keyDown+text+explicit char DOUBLE-fires (why m106 helper was wrong)",
              act(dbl, "button") == 2 and act(dbl, "form") == 2,
              f"button={act(dbl, 'button')} form={act(dbl, 'form')}")

        # Acceptance 1: press() must now produce exactly one real default action.
        check("press('Enter') activates a focused button exactly once",
              act(new, "button") == 1, f"activations={act(new, 'button')}")
        check("press('Enter') submits an implicit form exactly once",
              act(new, "form") == 1, f"activations={act(new, 'form')}")

        # Space: the `key` VALUE trap ("Space" vs " ").
        tab.navigate(uri)
        tab.sleep(0.2)
        tab.evaluate("window.__clicks = 0; document.getElementById('btn').focus()")
        tab.press("Space")
        tab.sleep(0.2)
        space_btn = tab.evaluate("window.__clicks")
        check("press('Space') activates a focused button exactly once",
              space_btn == 1, f"activations={space_btn}")

        tab.navigate(uri)
        tab.sleep(0.2)
        tab.evaluate("window.__clicks = 0; document.getElementById('btn').focus()")
        base = {"key": "Space", "code": "Space", "windowsVirtualKeyCode": 32,
                "nativeVirtualKeyCode": 32, "modifiers": 0}
        raw(tab, "rawKeyDown", **base)
        raw(tab, "keyUp", **base)
        tab.sleep(0.2)
        wrong_key = tab.evaluate("window.__clicks")
        check("key='Space' (the wrong value) stays inert — the trap this fixes",
              wrong_key == 0, f"activations={wrong_key}")

        # Space must not eat the space when it should type one.
        tab.navigate(uri)
        tab.sleep(0.2)
        tab.evaluate(
            "document.getElementById('txt').focus();"
            "document.getElementById('txt').value=''")
        tab.press("Space")
        tab.sleep(0.2)
        typed = tab.evaluate("document.getElementById('txt').value")
        check("press('Space') types a space into a focused input",
              typed == " ", f"value={typed!r}")

        # Tab: the claim in the bug report. Measured fresh, every run.
        moves = []
        for _ in range(4):
            tab.navigate(uri)
            tab.sleep(0.2)
            before = tab.evaluate(
                "document.activeElement.tagName + '#' + (document.activeElement.id || '')")
            tab.press("Tab")
            after = tab.evaluate(
                "document.activeElement.tagName + '#' + (document.activeElement.id || '')")
            moves.append((before, after))
        moved = sum(1 for a, z in moves if a != z)
        check("press('Tab') moves focus (bug report claimed it did not)",
              moved == len(moves),
              f"{moved}/{len(moves)} presses moved focus: {[f'{a}->{z}' for a, z in moves]}")

        # Tab out of <body> specifically, the phrasing the report used.
        tab.navigate(uri)
        tab.sleep(0.2)
        tab.evaluate("document.body.focus()")
        tab.press("Tab")
        tab.sleep(0.15)
        from_body = tab.evaluate(
            "document.activeElement.tagName + '#' + (document.activeElement.id || '')")
        check("press('Tab') from <body> leaves the body",
              from_body != "BODY#", f"activeElement={from_body}")

        # Escape must still reach a keydown listener (React dialogs depend on it).
        tab.navigate(uri)
        tab.sleep(0.2)
        tab.evaluate("window.__keys = []; document.getElementById('txt').focus()")
        tab.press("Escape")
        tab.sleep(0.15)
        seen = cast(list, tab.evaluate("window.__keys"))
        check("press('Escape') reaches a keydown listener",
              "Escape" in seen, f"keys={seen}")

        # Arrow keys: m102 measures horizontal scroll with these.
        tab.navigate(uri)
        tab.sleep(0.2)
        tab.evaluate(
            "window.__keys = []; document.getElementById('scroller').focus();"
            "document.getElementById('scroller').scrollLeft = 0"
        )
        for _ in range(5):
            tab.press("ArrowRight")
        tab.sleep(0.3)
        scroll_left = tab.evaluate("document.getElementById('scroller').scrollLeft")
        arrow_keys = cast(list, tab.evaluate("window.__keys"))
        check("press('ArrowRight') x5 scrolls a focused region (m102's contract)",
              scroll_left > 0 and "ArrowRight" in arrow_keys,
              f"scrollLeft={scroll_left} keys={arrow_keys[-6:]}")

        # type_text: every glyph exactly once (the companion double-insert bug).
        tab.navigate(uri)
        tab.sleep(0.2)
        tab.evaluate(
            "document.getElementById('txt').focus();"
            "document.getElementById('txt').value=''")
        tab.type_text("probe-extra")
        tab.sleep(0.2)
        typed = tab.evaluate("document.getElementById('txt').value")
        check("type_text() inserts each glyph exactly once",
              typed == "probe-extra", f"value={typed!r}")

        # Driver dispatch bookkeeping: press() must be using the table above.
        check("driver sends text for Enter/Space and none for Tab",
              key_text("Enter") == "\r" and key_text("Space") == " "
              and key_text("Tab") == "" and key_text("Escape") == "",
              f"Enter={key_text('Enter')!r} Space={key_text('Space')!r} "
              f"Tab={key_text('Tab')!r}")

        if keep:
            shot = str(HERE / "press-regression.png")
            tab.navigate(uri)
            tab.sleep(0.3)
            tab.screenshot(shot)
            print(f"screenshot: {shot}")
    finally:
        b.close()

    out = HERE / "press-regression.json"
    out.write_text(json.dumps({
        "page": "file://…/page.html (generated per run)",
        "recipes": rows,
        "checks": CHECKS,
    }, indent=2), encoding="utf-8")
    failed = [c for c in CHECKS if not c["ok"]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed; "
          f"json -> {out}")
    if failed:
        for c in failed:
            print(f"  FAILED: {c['check']} — {c['detail']}")
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="also save a screenshot")
    args = ap.parse_args()
    sys.exit(run(args.keep))
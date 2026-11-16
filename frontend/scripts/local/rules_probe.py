#!/usr/bin/env python3
"""M82-F2 probe: the /rules rule-editor row at mobile widths.

Reproduces QA finding F2 (the `Enabled` toggle + label clipped by the right
screen edge, and the `?` HelpHint overflowing its label parent) against a
locally-built bundle, and reports the numbers the acceptance criteria pin.

Run:
  python3 frontend/scripts/local/rules_probe.py --base http://127.0.0.1:4173 \
      --viewports 412,360,320 --fonts 16,18.4,20.8 --json out.json

Acceptance (task t_6434b212):
  * button.inline-flex...gap-2.5 (text `Enabled`) right <= viewport width - 8
  * every `?` HelpHint button inside its label parent
  * 0 clipped nodes on /rules
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "a11y"),
)


def _free_port() -> int:
    """a11y_driver reads A11Y_CDP_PORT at import time and 9455 is shared with
    whatever else on this box is driving a browser — bind an ephemeral port
    first so this probe never attaches to a stranger's Chrome."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


os.environ["A11Y_CDP_PORT"] = str(_free_port())
os.environ["A11Y_PROFILE_DIR"] = os.path.join(
    tempfile.gettempdir(), f"fintrack-rules-{os.getpid()}"
)

from a11y_driver import Browser  # noqa: E402

PIXEL_UA = (
    "Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
)

SEL = r"""
(el) => {
  if (!el || !el.tagName) return "";
  let s = el.tagName.toLowerCase();
  if (el.id) s += "#" + el.id;
  const cls = (typeof el.className === "string" ? el.className : "").trim()
    .split(/\s+/).filter(Boolean);
  if (cls.length) s += "." + cls.slice(0, 6).join(".");
  return s;
}
"""

MEASURE = r"""
(() => {
  const vw = document.documentElement.clientWidth;
  const sel = (el) => {
    if (!el || !el.tagName) return "";
    let s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    const cls = (typeof el.className === "string" ? el.className : "").trim()
      .split(/\s+/).filter(Boolean);
    if (cls.length) s += "." + cls.slice(0, 6).join(".");
    return s;
  };
  const scrollerAncestor = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === "auto" || ox === "scroll") return sel(p);
    }
    return null;
  };
  const hidden = (el) => {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return true;
    if (el.closest('[aria-hidden="true"]')) return true;
    if (el.classList.contains("sr-only")) return true;
    return false;
  };

  // 1. nodes whose box leaves the viewport with no scroller to reach them
  const clipped = [];
  for (const el of document.querySelectorAll("main *")) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (hidden(el)) continue;
    if (r.right <= vw + 0.5 && r.left >= -0.5) continue;
    if (scrollerAncestor(el)) continue;
    clipped.push({
      sel: sel(el),
      text: (el.innerText || "").trim().slice(0, 30),
      left: Math.round(r.left * 10) / 10,
      right: Math.round(r.right * 10) / 10,
      overBy: Math.round((r.right - vw) * 10) / 10,
    });
  }
  clipped.sort((a, b) => b.right - a.right);

  // 2. children sticking out of their own parent box
  const parentOverflow = [];
  for (const el of document.querySelectorAll("main *")) {
    const p = el.parentElement;
    if (!p) continue;
    const r = el.getBoundingClientRect();
    const pr = p.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (hidden(el) || hidden(p)) continue;
    // A horizontal scroller is allowed to hold wider content than its parent.
    if (scrollerAncestor(el)) continue;
    const over = r.right - pr.right;
    if (over > 0.5) {
      parentOverflow.push({
        sel: sel(el),
        text: (el.innerText || "").trim().slice(0, 30),
        parent: sel(p),
        parentRight: Math.round(pr.right * 10) / 10,
        overRight: Math.round(over * 10) / 10,
      });
    }
  }
  parentOverflow.sort((a, b) => b.overRight - a.overRight);

  // 3. the rule-editor form row geometry
  const row = document.querySelector("form .grid.grid-cols-1");
  let grid = null;
  if (row) {
    const cs = getComputedStyle(row);
    const rr = row.getBoundingClientRect();
    grid = {
      rect: {x: Math.round(rr.x*10)/10, right: Math.round(rr.right*10)/10,
             w: Math.round(rr.width*10)/10},
      template: cs.gridTemplateColumns,
      gap: cs.columnGap,
      children: [...row.children].map((c) => {
        const cr = c.getBoundingClientRect();
        return {
          cls: (c.className || "").toString(),
          col: getComputedStyle(c).gridColumn,
          x: Math.round(cr.x*10)/10, right: Math.round(cr.right*10)/10,
          w: Math.round(cr.width*10)/10,
          scrollW: c.scrollWidth, clientW: c.clientWidth,
        };
      }),
    };
  }

  // 4. the specific acceptance targets
  const toggle = [...document.querySelectorAll("button")]
    .find((b) => (b.innerText || "").trim() === "Enabled");
  let toggleCheck = null;
  if (toggle) {
    const r = toggle.getBoundingClientRect();
    toggleCheck = {
      x: Math.round(r.x*10)/10, right: Math.round(r.right*10)/10,
      w: Math.round(r.width*10)/10,
      limit: vw - 8,
      pass: r.right <= vw - 8,
      minW: getComputedStyle(toggle).minWidth,
    };
  }
  const hints = [...document.querySelectorAll("button.cursor-help")].map((b) => {
    const r = b.getBoundingClientRect();
    const p = b.parentElement;
    const pr = p.getBoundingClientRect();
    return {
      text: (b.innerText || "").trim(),
      right: Math.round(r.right*10)/10,
      parent: sel(p),
      parentRight: Math.round(pr.right*10)/10,
      overRight: Math.round((r.right - pr.right)*10)/10,
      pass: r.right <= pr.right + 0.5,
    };
  });

  return {
    vw,
    docScrollW: document.documentElement.scrollWidth,
    pageHScroll: document.documentElement.scrollWidth >
                 document.documentElement.clientWidth + 1,
    rootFont: getComputedStyle(document.documentElement).fontSize,
    clippedCount: clipped.length,
    clipped: clipped.slice(0, 20),
    parentOverflowCount: parentOverflow.length,
    parentOverflow: parentOverflow.slice(0, 20),
    grid,
    toggle: toggleCheck,
    hints,
  };
})()
"""

READY = (
    "!document.querySelector('[data-testid=\"route-fallback\"]') && "
    "!!document.querySelector('main') && document.querySelector('main').innerText.length > 60"
)


def login(tab, base: str) -> None:
    tab.navigate(f"{base}/login")
    tab.wait_for("document.readyState === 'complete'", timeout=30)
    expr = """
    (() => {
      const btn = [...document.querySelectorAll('button')].find(b => b.innerText.includes('Explore the demo'));
      if (!btn) return null;
      const r = btn.getBoundingClientRect();
      return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    })()
    """
    for attempt in range(3):
        for _ in range(40):
            box = tab.evaluate(expr)
            if box:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("demo button never appeared")
        tab.click_at(box["x"], box["y"])
        try:
            tab.wait_for("location.pathname === '/dashboard'", timeout=30)
            return
        except TimeoutError as err:
            print(f"login attempt {attempt + 1} failed: {err}", file=sys.stderr)
            tab.navigate(f"{base}/login")
            tab.wait_for("document.readyState === 'complete'", timeout=30)
            time.sleep(1.0)
    raise RuntimeError("demo login never reached /dashboard")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:4173")
    ap.add_argument("--viewports", default="412,400,393,384,375,360,344,320")
    ap.add_argument("--height", type=int, default=915)
    ap.add_argument("--fonts", default="", help="comma list of root font px, e.g. 16,18.4,20.8")
    ap.add_argument("--themes", default="dark")
    ap.add_argument("--json", default=None)
    ap.add_argument("--shots", default=None)
    args = ap.parse_args()

    widths = [int(w) for w in args.viewports.split(",") if w]
    fonts = [float(f) for f in args.fonts.split(",") if f] or [16]
    themes = [t for t in args.themes.split(",") if t]

    b = Browser(width=widths[0], height=args.height)
    out = {"base": args.base, "runs": []}
    try:
        tab = b.new_tab("about:blank")
        tab.enable_runtime()
        tab.send(
            "Emulation.setUserAgentOverride",
            {"userAgent": PIXEL_UA, "platform": "Android",
             "acceptLanguage": "en-CA,en"},
        )
        tab.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": widths[0], "height": args.height, "deviceScaleFactor": 2.75,
             "mobile": True, "screenWidth": widths[0], "screenHeight": args.height},
        )
        login(tab, args.base)

        for theme in themes:
            for w in widths:
                for font in fonts:
                    tab.send(
                        "Emulation.setDeviceMetricsOverride",
                        {"width": w, "height": args.height, "deviceScaleFactor": 2.75,
                         "mobile": True, "screenWidth": w, "screenHeight": args.height},
                    )
                    ready = False
                    for attempt in range(3):
                        try:
                            tab.navigate(f"{args.base}/rules")
                            if tab.evaluate("location.pathname") == "/login":
                                login(tab, args.base)
                                tab.navigate(f"{args.base}/rules")
                            tab.wait_for(READY, timeout=30)
                            ready = True
                            break
                        except Exception:
                            # The shared demo session rotates mid-sweep, and a
                            # bounce or an error panel leaves no <main> —
                            # re-sign-in rather than aborting the whole ladder.
                            print(
                                f"not ready {w}px font={font} theme={theme} "
                                f"attempt {attempt + 1}: path="
                                f"{tab.evaluate('location.pathname')} body="
                                f"{str(tab.evaluate('(document.body.innerText || \'\').slice(0,160)'))}",
                                file=sys.stderr,
                            )
                            time.sleep(1.5)
                    if not ready:
                        out["runs"].append(
                            {"theme": theme, "width": w, "font": font,
                             "error": "page never became ready"}
                        )
                        print(f"{theme:>5} {w:>4}px font={font:<5} NOT-READY FAIL")
                        continue
                    tab.evaluate(
                        f"localStorage.setItem('theme', {json.dumps(theme)});"
                        f"document.documentElement.classList.toggle('dark', "
                        f"{json.dumps(theme)} === 'dark');"
                    )
                    tab.evaluate(
                        "document.documentElement.style.fontSize = "
                        f"{json.dumps(str(font) + 'px')}"
                    )
                    tab.sleep(1.2)
                    m = tab.evaluate(MEASURE)
                    m.update({"theme": theme, "width": w, "font": font})
                    out["runs"].append(m)
                    if args.shots:
                        os.makedirs(args.shots, exist_ok=True)
                        tab.screenshot(
                            os.path.join(
                                args.shots,
                                f"rules_{w}_font{font}_{theme}.png",
                            )
                        )
                    ok = (m["toggle"] or {}).get("pass") and m["clippedCount"] == 0 and all(
                        h["pass"] for h in m["hints"]
                    )
                    tg = m["toggle"]
                    print(
                        f"{theme:>5} {w:>4}px font={font:<5} "
                        f"clipped={m['clippedCount']:<3} "
                        f"parentOver={m['parentOverflowCount']:<3} "
                        f"toggle={'-' if not tg else str(tg['right']) + '/' + str(tg['limit'])} "
                        f"hints={'ok' if all(h['pass'] for h in m['hints']) else 'OVER'} "
                        f"{'PASS' if ok else 'FAIL'}"
                    )
    finally:
        b.close()

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)
        print("wrote", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

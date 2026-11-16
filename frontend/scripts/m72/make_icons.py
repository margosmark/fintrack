#!/usr/bin/env python3
"""Render the fin_tracker PWA icons (M72).

Generates `frontend/public/icons/*.png` from one SVG source by driving the
same headless-Chrome CDP harness the a11y audit uses (AGENTS §5 — no image
toolchain is installed on this box, and rendering the real glyph beats a
hand-drawn approximation).

Run:  python3 make_icons.py
Outputs (committed — the manifest and tests reference them):
  icons/icon-192.png          192x192, rounded corners, transparent corners
  icons/icon-512.png          512x512, same
  icons/icon-maskable-512.png 512x512, full-bleed, glyph inside the 80% safe zone
  icons/apple-touch-icon.png  180x180, opaque (iOS masks it itself)
"""
from __future__ import annotations

import base64
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.abspath(os.path.join(HERE, "..", ".."))
ICONS = os.path.join(FRONTEND, "public", "icons")
sys.path.insert(0, os.path.join(FRONTEND, "scripts", "a11y"))

from a11y_driver import Browser  # noqa: E402

# lucide `zap` (the brand glyph AppShell renders), filled on a gradient.
ZAP = (
    "M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7"
    "a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"
)

SURFACE = "#090c12"  # --color-surface, dark (the hero theme)
ACCENT_A = "#22d3ee"  # --color-accent
ACCENT_B = "#5eead4"  # --color-accent-soft


def svg(size: int, *, maskable: bool, opaque: bool) -> str:
    """One icon frame. `maskable` drops the rounding and shrinks the glyph
    into the 80% safe circle; `opaque` fills the corners (iOS touch icons are
    composited on white otherwise)."""
    radius = 0 if maskable else round(size * 0.1875)
    glyph = 0.48 if maskable else 0.62
    g = size * glyph
    pad = (size - g) / 2
    corner = "" if opaque else ""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">
  <defs>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{ACCENT_A}"/>
      <stop offset="1" stop-color="{ACCENT_B}"/>
    </linearGradient>
    <rect id="plate" x="0" y="0" width="{size}" height="{size}" rx="{radius}" fill="{SURFACE}"/>
  </defs>
  {corner}<use href="#plate"/>
  <g transform="translate({pad},{pad}) scale({g / 24:.6f})">
    <path d="{ZAP}" fill="url(#brand)"/>
  </g>
</svg>"""


PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;background:transparent;overflow:hidden}
#frame{width:%(w)dpx;height:%(h)dpx}
</style></head><body><div id="frame">%(svg)s</div></body></html>"""


def render(tab, markup: str, size: int, out: str) -> None:
    path = os.path.join(ICONS, ".frame.html")
    with open(path, "w") as fh:
        fh.write(PAGE % {"w": size, "h": size, "svg": markup.replace("<svg ", '<svg style="display:block" ')})
    tab.navigate("file://" + path)
    tab.wait_for("document.readyState === 'complete'")
    tab.send("Emulation.setDefaultBackgroundColorOverride",
             {"color": {"r": 0, "g": 0, "b": 0, "a": 0}})
    res = tab.send("Page.captureScreenshot", {
        "format": "png",
        "captureBeyondViewport": True,
        "clip": {"x": 0, "y": 0, "width": size, "height": size, "scale": 1},
    })
    with open(out, "wb") as fh:
        fh.write(base64.b64decode(res["data"]))
    size_on_disk = os.path.getsize(out)
    # PNG magic + declared size in the IHDR
    with open(out, "rb") as fh:
        head = fh.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", out
    w = int.from_bytes(head[16:20], "big")
    h = int.from_bytes(head[20:24], "big")
    assert (w, h) == (size, size), (out, w, h)
    print(f"  {os.path.relpath(out, FRONTEND)}  {w}x{h}  {size_on_disk} B")


def main() -> int:
    os.makedirs(ICONS, exist_ok=True)
    targets = [
        ("icon-192.png", 192, dict(maskable=False, opaque=False)),
        ("icon-512.png", 512, dict(maskable=False, opaque=False)),
        ("icon-maskable-512.png", 512, dict(maskable=True, opaque=False)),
        ("apple-touch-icon.png", 180, dict(maskable=False, opaque=True)),
    ]
    b = Browser(width=600, height=600)
    try:
        tab = b.new_tab("about:blank")
        tab.enable_runtime()
        for name, size, kw in targets:
            render(tab, svg(size, **kw), size, os.path.join(ICONS, name))
    finally:
        b.close()
        stray = os.path.join(ICONS, ".frame.html")
        if os.path.exists(stray):
            os.remove(stray)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

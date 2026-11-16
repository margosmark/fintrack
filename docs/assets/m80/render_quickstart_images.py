#!/usr/bin/env python3
"""Render M80's committed quickstart evidence into guide images.

Source of truth is docs/assets/m80/quickstart-transcript.txt (M80's executed
run — register, mint token, tools/list, allowed call, refused write, receipts).
Nothing here is rewritten: the transcript is wrapped and painted, that's all.
"""
import os
from PIL import Image, ImageDraw, ImageFont

WS = "/home/hermes/.hermes/kanban/boards/fintrack/workspaces/t_304570f0"
SRC = WS + "/docs/assets/m80/quickstart-transcript.txt"
OUT_DIR = WS + "/frontend/public/help"
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]
FONT_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
]

BG = "#0d1420"
FG = "#d9e4f2"
MUTED = "#8fa3bf"
ACCENT = "#22d3ee"
GREEN = "#34d399"
RED = "#f87171"
AMBER = "#fbbf24"

FONT = next((f for f in FONT_PATHS if os.path.exists(f)), None)
FONTB = next((f for f in FONT_BOLD if os.path.exists(f)), FONT)
if not FONT:
    raise SystemExit("no mono font found")

WRAP = 104
CHAR_H = 16
PAD_X = 20
PAD_Y = 18


def wrap(line: str, width: int = WRAP) -> list[str]:
    if len(line) <= width:
        return [line]
    out, cur = [], ""
    for word in line.split(" "):
        if len(cur) + len(word) + 1 > width and cur:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out


def colors_for(line: str) -> str:
    s = line.strip()
    if s.startswith("$ "):
        return ACCENT
    if s.startswith("< "):
        return GREEN
    if s.startswith("#"):
        return MUTED
    if s.startswith("---"):
        return AMBER
    if s.startswith("isError=True") or '"error"' in s or "403" in s:
        return RED
    return FG


def render(lines: list[str], path: str, title: str) -> None:
    rows: list[tuple[str, str]] = []
    for line in lines:
        for piece in wrap(line):
            rows.append((piece, colors_for(line)))
    width = max(len(p[0]) for p in rows)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    bbox = probe.textbbox((0, 0), "M", font=ImageFont.truetype(FONT, 13))
    char_w = bbox[2] - bbox[0] + 1.6
    img_w = int(PAD_X * 2 + char_w * width)
    img_h = PAD_Y * 2 + CHAR_H * (len(rows) + 3)
    img = Image.new("RGB", (img_w, img_h), BG)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 13)
    bold = ImageFont.truetype(FONTB, 13)
    title_font = ImageFont.truetype(FONTB, 13)

    y = PAD_Y
    draw.text((PAD_X, y), title, font=title_font, fill=MUTED)
    y += CHAR_H * 2
    for text, color in rows:
        draw.text((PAD_X, y), text, font=font, fill=color)
        y += CHAR_H
    img.save(path)
    print(os.path.basename(path), img.size, f"{len(rows)} lines")


def main() -> None:
    with open(SRC) as fh:
        lines = fh.read().splitlines()
    split = next(i for i, l in enumerate(lines) if l.startswith("--- receipts"))
    os.makedirs(OUT_DIR, exist_ok=True)
    render(
        lines[:split],
        OUT_DIR + "/agent-run.png",
        "$ M80 executed quickstart — mint a token, list tools, allow a read, refuse a write",
    )
    render(
        lines[split:],
        OUT_DIR + "/agent-receipts.png",
        "# receipts: one row per tool call, yours alone to read (GET /api/v1/mcp-audit)",
    )


if __name__ == "__main__":
    main()

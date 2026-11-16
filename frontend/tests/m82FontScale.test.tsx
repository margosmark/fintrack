// M82 F7 — Android max-font (200% root = 32px) regression for the bottom tab
// bar (QA `MOBILE_QA_REPORT.md`, repro `m82/round8.py` → `m82/out/font200.json`).
//
// M185 (docs/47 §1.2) retired MobileNav's SIX tabs for the five-tab
// `MobileTabBar` (Home · Activity · Add · Portfolio · More), with `Add`
// promoted to the centre. The two pinned rules of this file survive the
// redesign unchanged, and the arithmetic only improved (5 x 44 = 220 fits a
// 264px viewport; the old 6 x 88 = 528 did not fit 412):
//
//   1. the touch floor is in PIXELS (`min-w-[44px]` / `min-h-[44px]`) — 44 is
//      a physical hit-target size, it must not scale with the user's font
//      setting;
//   2. the label is capped at `min(0.75rem, 14px)` and ellipsized inside the
//      tab (`max-w-full truncate`), so at 200% it stays at a width a 1/5
//      column can hold instead of forcing the tab wider than the viewport.
//
// jsdom has no layout engine, so the x-measurement itself is browser
// evidence; this test pins the SOURCE of that fix so a class rename or a
// "cleanup" cannot silently restore the rem-based floor.
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { MobileTabBar } from "../src/components/shell/MobileTabBar";

const source = readFileSync(
  path.join(process.cwd(), "src", "components", "shell", "MobileTabBar.tsx"),
  "utf8",
);
// Comments explain the old defect — strip them so the negative assertions
// below can only ever match CODE (the fix's own comment quotes `min-w-11`).
const code = source
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/\/\/.*$/gm, "");

function renderNav() {
  return render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <MobileTabBar onAdd={() => {}} onMore={() => {}} moreOpen={false} />
    </MemoryRouter>,
  );
}

describe("M82 F7 — bottom nav at 200% root font (M185 five tabs)", () => {
  it("renders the five-tab bar: three route tabs, centre Add, trailing More", () => {
    renderNav();
    const nav = document.querySelector(".mobile-bottom-nav");
    expect(nav).not.toBeNull();
    const links = nav!.querySelectorAll("a");
    expect(links).toHaveLength(3);
    for (const label of ["Home", "Activity", "Portfolio"]) {
      expect(screen.getByRole("link", { name: new RegExp(label) })).toBeTruthy();
    }
    // Add is a REAL control (docs/47 §1.2) — a button, not a dead stub.
    expect(screen.getByRole("button", { name: "Add transaction" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /More/ })).toBeTruthy();
  });

  it("keeps the touch floor in physical pixels, never in rem", () => {
    // `min-w-11` = 2.75rem = 88px at root 32 — the exact defect. It must not
    // come back in ANY utility form (min-w-11, min-w-[2.75rem], min-w-[3.5vw]…).
    expect(code).not.toMatch(/min-w-\s*(?:11|\[2\.75rem\])/);
    expect(code).toContain("min-w-[44px]");
    expect(code).toContain("min-h-[44px]");
  });

  it("caps and ellipsizes the label so it cannot widen the tab", () => {
    // The route-tab label span (bound `{tab.label}`), not a literal label.
    const span = /<span className="([^"]*)">\s*\{tab\.label\}/.exec(code);
    expect(span).not.toBeNull();
    expect(span![1]).toContain("max-w-full");
    expect(span![1]).toContain("truncate");
    // Cap = min(0.75rem, 14px): identical to text-xs below ~187% root font,
    // bounded above so "Portfolio" stays inside a 1/5 column at 200%.
    expect(span![1]).toContain("text-[length:min(0.75rem,14px)]");
  });

  it("clears the iOS gesture bar", () => {
    expect(code).toContain("env(safe-area-inset-bottom)");
  });
});

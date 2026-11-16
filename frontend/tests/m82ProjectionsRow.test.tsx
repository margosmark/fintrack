// M82 F4 — the /reports → Projections scenario row must be allowed to wrap
// (QA `MOBILE_QA_REPORT.md` F4, repro 360×800 Pixel 10 → `m82/out/followup.log`).
//
// The row is a flex line of FIXED-width controls: colour dot (8px) + label
// input (`w-44` = 176) + return input (`w-24` = 96) + the `% / yr` unit + a
// 44px icon-only remove button ≈ 391px. The card's content column is only
// 286px at 360px viewport and 248px at 320px, `main#main-content` is
// `overflow-x: clip`, and the page never scrolls horizontally — so with
// `flex-nowrap` the overflow had nowhere to go and the remove button was
// parked at x=361..405 in a 360px viewport: 5 nodes fully past the edge,
// `fullyOffRight=5`, delete unreachable. The `% / yr` unit was also squeezed
// to its 12px min-content by the negative free space.
//
// `flex-wrap` is the whole fix: one line while it fits (>= ~463px, where
// 391 + the card's `p-4` + the page's `p-5` still fit), two lines below that,
// with the return input, its unit and the remove button moving together.
//
// jsdom has no layout engine, so the x=361..405 measurement itself is browser
// evidence (CDP probe: `fullyOffRight == 0` at 360/320/412, remove button
// centre hit-tests to itself, `document.scrollWidth == innerWidth`). These
// tests pin the SOURCE of that fix so a class rename or a "tidy the row up"
// cleanup cannot silently put the nowrap back.
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import path from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../src/theme/ThemeProvider";
import { ToastProvider } from "../src/components/ui/Toast";
import { ProjectionsPanel } from "../src/components/reports/ProjectionsPanel";

const a = vi.hoisted(() => ({ user: null as { email: string } | null }));

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({ user: a.user }),
}));
vi.mock("../src/api/projections", () => ({
  getProjectionDefaults: vi.fn(),
  // M157: the run is server-computed, so the panel imports this too — it must
  // exist on the mock even though this file never clicks "Run projections".
  runProjections: vi.fn(),
}));
vi.mock("../src/lib/projectionEngine", () => ({
  ProjectionInputError: class ProjectionInputError extends Error {},
  runProjectionEngine: vi.fn(),
}));

// jsdom has no matchMedia; ThemeProvider calls it.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() {
      return false;
    },
  })) as unknown as typeof window.matchMedia;
}

const source = readFileSync(
  path.join(
    process.cwd(),
    "src",
    "components",
    "reports",
    "ProjectionsPanel.tsx",
  ),
  "utf8",
);
// The fix's own comment quotes `flex-wrap` — strip comments so the source
// assertions below can only ever match CODE.
const code = source
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/\/\/.*$/gm, "");

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <ThemeProvider>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <ProjectionsPanel />
        </ToastProvider>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

/** The flex line that owns a row's remove button (its closest div). */
function scenarioRow(container: HTMLElement, index = 0): HTMLElement {
  const remove = container.querySelectorAll('[aria-label^="Remove "]');
  expect(remove.length).toBeGreaterThan(index);
  const row = remove[index]!.closest("div");
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

beforeEach(async () => {
  localStorage.clear();
  localStorage.setItem("theme", "light");
  a.user = null;
  vi.clearAllMocks();
  // A resolved default keeps React Query quiet (an unresolved mock leaves the
  // key at undefined and Vitest prints a stderr warning on an otherwise green
  // file — a green count is not a green gate if the run reports noise).
  const { getProjectionDefaults } = await import("../src/api/projections");
  vi.mocked(getProjectionDefaults).mockResolvedValue({ start_value_cad: null });
});

describe("M82 F4 — Projections scenario row wraps instead of overflowing", () => {
  it("gives every scenario row a wrapping flex line", () => {
    const { container } = renderPanel();
    const row = scenarioRow(container);
    const classes = row.className.split(/\s+/);
    expect(classes).toContain("flex");
    expect(classes).toContain("flex-wrap");
    expect(classes).not.toContain("flex-nowrap");
  });

  it("wraps every row after a preset chip adds a second scenario", async () => {
    const { getProjectionDefaults } = await import("../src/api/projections");
    vi.mocked(getProjectionDefaults).mockResolvedValue({ start_value_cad: null });

    const { container } = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Aggressive/ }));
    fireEvent.click(screen.getByRole("button", { name: /Conservative/ }));
    // Three scenarios (Balanced default + two presets), three wrapping rows.
    expect(container.querySelectorAll('[aria-label^="Remove "]')).toHaveLength(3);
    for (let i = 0; i < 3; i += 1) {
      expect(scenarioRow(container, i).className.split(/\s+/)).toContain("flex-wrap");
    }
  });

  it("keeps the row's controls at their fixed widths (the wrap carries the fit, not shrink-to-nothing)", () => {
    const { container } = renderPanel();
    const inputs = scenarioRow(container).querySelectorAll("input");
    expect(inputs).toHaveLength(2);
    expect(inputs[0]!.className).toContain("w-44");
    expect(inputs[1]!.className).toContain("w-24");
  });

  it("pins the source: the scenario row is a wrapping flex row, never nowrap", () => {
    // The row is the div rendered directly inside scenarios.map(...) and it is
    // the only flex row in the file that holds the remove button.
    expect(code).toMatch(/<div key=\{index\} className="flex flex-wrap items-center gap-2">/);
    expect(code).not.toContain("flex-nowrap");
    // The preset-chip row already wrapped; it is not the defect.
    expect(code.match(/flex flex-wrap items-center gap-2/g)?.length).toBe(2);
  });
});

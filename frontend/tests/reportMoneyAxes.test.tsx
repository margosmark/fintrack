// M67 follow-up (t_24a00e09): the two Reports money Y-axes that still rendered
// raw ticks outside `chrome.moneyAxis`.
//
//   DividendsPanel   `$${v}`            -> `$16000` / `$-55000`
//   ProjectionsPanel `$${v.toLocale…}`  -> `$1,200,000` on an 80px axis
//
// Both now spread `chrome.moneyAxis` (formatAxisMoney), so the tick strings are
// compact and signed at >= $10K with the sign on the outside (docs/09 "Money
// axis is compact and signed").
//
// These are behaviour assertions: the panels are RENDERED with fixture data and
// the tick strings are read back out of the SVG the axes actually drew, plus a
// pin on the REACTIVE formatter contract (F-7, docs/46 §D3): the formatter the
// panel hands Recharts formats exactly like `formatAxisMoney`, and its
// IDENTITY changes across a mask flip — that new prop identity is what makes
// Recharts' `axisPropsAreEqual` memo miss and the tick layer repaint. (Before
// F-7 this file pinned `tickFormatter` by identity against a separately
// rendered hook's `moneyAxis`; that only held while `moneyAxis` was the stable
// module function — the very reference that froze the axis.)

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { act, fireEvent, render, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../src/theme/ThemeProvider";
import type { Theme } from "../src/theme/ThemeProvider";
import { useChartChrome } from "../src/components/charts/chartDefaults";
import { DividendsPanel } from "../src/components/reports/DividendsPanel";
import { ProjectionsPanel } from "../src/components/reports/ProjectionsPanel";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";

// jsdom has no matchMedia; ThemeProvider and useReducedMotion both call it.
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

// The panels fetch their own data; hand them deterministic fixtures.
// M160-R2 (t_85311c34): getDividendReport is a thin read of
// `GET /reports/dividends`, back in `api/reports.ts` with the rest.
vi.mock("../src/api/reports", () => ({ getDividendReport: vi.fn() }));
vi.mock("../src/api/projections", () => ({
  getProjectionDefaults: vi.fn(),
  // M157: the run is server-computed, so the panel renders from this one.
  runProjections: vi.fn(),
}));

// The panel guards its read-only toast with useAuth/useToast — neither has a
// provider in this harness, so provide module-level stubs (user: null => no
// toast, matching production's owner tenant).
vi.mock("../src/auth/AuthContext", () => ({ useAuth: () => ({ user: null }) }));
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));
// The client mirror stays as the offline/E2EE fallback (docs/33 §Server-first
// rule 5) but this panel no longer asks it to compute.
vi.mock("../src/lib/projectionEngine", () => ({
  ProjectionInputError: class ProjectionInputError extends Error {},
  runProjectionEngine: vi.fn(),
}));

// Recharts' ResponsiveContainer measures the DOM, which jsdom cannot do (see
// donut.test.ts); give every chart a fixed box, and record the tickFormatter
// each <YAxis> actually received so the panel's axis can be pinned to
// chrome.moneyAxis by identity.
const captured = vi.hoisted(() => ({
  yAxes: [] as Array<{ tickFormatter?: (v: number) => string }>,
}));

vi.mock("recharts", async () => {
  const mod = await import("recharts");
  const ReactMod = await import("react");
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => {
      const child = Array.isArray(children) ? children[0] : children;
      if (child && typeof child === "object" && "type" in (child as object)) {
        return ReactMod.cloneElement(child as React.ReactElement, {
          width: 480,
          height: 320,
        });
      }
      return child;
    },
    YAxis: (props: React.ComponentProps<typeof mod.YAxis>) => {
      captured.yAxes.push(props as { tickFormatter?: (v: number) => string });
      return ReactMod.createElement(mod.YAxis, props);
    },
  };
});

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <ThemeProvider>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </ThemeProvider>
  );
}

/** Tick strings drawn on a panel's left (money) axis. */
function moneyTicks(container: HTMLElement): string[] {
  // Recharts v3 renders tick labels in a sibling layer, NOT inside the
  // `.recharts-yAxis` group, so the descendant selector finds nothing.
  return [...container.querySelectorAll<SVGTextElement>(".recharts-yAxis-tick-labels text")].map(
    (t) => t.textContent ?? "",
  );
}

/** `$16000` / `$-55000` — four-or-more digits with no grouping: the raw tick. */
const RAW_MONEY_TICK = /^\$-?\d{4,}$/;
/** `$1,200,000` — grouped past the thousands: correct below $10K only. */
const GROUPED_MILLION_TICK = /,\d{3},\d{3}/;

/** Every .ts/.tsx source file under `dir` (cwd is the vitest root, frontend/). */
function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("theme", "light" satisfies Theme);
  sessionStorage.clear();
  initMaskForSession(); // each test starts unmasked (boot contract)
  captured.yAxes.length = 0;
  vi.clearAllMocks();
});

// --- fixtures ---------------------------------------------------------------

const DIVIDENDS = {
  year: 2025,
  available_years: [2025, 2024],
  items: [
    {
      security_id: 1,
      symbol: "VDY",
      name: "VDY",
      dividend_cad: "16000.00",
      distribution_cad: "0.00",
      roc_cad: "0.00",
      total_cad: "16000.00",
      payment_count: 12,
      last_payment_date: "2025-12-15",
    },
  ],
  by_month: [
    { month: "2025-01", total_cad: "0.00" },
    { month: "2025-02", total_cad: "4000.00" },
    { month: "2025-03", total_cad: "8000.00" },
    { month: "2025-04", total_cad: "12000.00" },
    { month: "2025-05", total_cad: "16000.00" },
  ],
  totals: {
    dividend_cad: "16000.00",
    distribution_cad: "0.00",
    roc_cad: "0.00",
    total_cad: "16000.00",
  },
  ttm_total_cad: "16000.00",
  unresolved_fx: 0,
};

function series(values: number[]) {
  return values.map((value_cad, i) => ({
    year: 2026 + i,
    contributed_cad: "0.00",
    value_cad: String(value_cad),
    value_real_cad: String(value_cad),
  }));
}

const PROJECTION = {
  start_value_cad: "0",
  years: 5,
  inflation_pct: "2",
  scenarios: [
    {
      label: "Balanced",
      series: series([0, 300000, 600000, 900000, 1200000]),
      final_value_cad: "1200000.00",
      total_contributed_cad: "0.00",
      total_gain_cad: "1200000.00",
    },
  ],
};

describe("Reports money Y-axes use chrome.moneyAxis", () => {
  it("DividendsPanel: monthly ticks are compact and signed, not raw", async () => {
    const { getDividendReport } = await import("../src/api/reports");
    vi.mocked(getDividendReport).mockResolvedValue(DIVIDENDS as never);

    const { container } = render(<DividendsPanel />, { wrapper });
    await waitFor(() => expect(moneyTicks(container).length).toBeGreaterThan(0));
    const ticks = moneyTicks(container);

    // The fixture peaks at 16000, so the axis draws its domain top there.
    expect(ticks).toContain("$16K");
    // The pre-fix tick (`$${v}`) was raw: $16000 / $-55000.
    expect(ticks).not.toContain("$16000");
    expect(ticks.filter((t) => RAW_MONEY_TICK.test(t))).toEqual([]);

    // The axis the panel handed to Recharts formats like the ONE exported
    // money formatter (F-7: pinned by behaviour — the reference itself is now
    // a per-render reactive closure, see the header).
    const formatter = captured.yAxes.at(-1)?.tickFormatter as
      | ((v: number) => string)
      | undefined;
    expect(formatter).toBeDefined();
    // ...so the representative values the M67 review called out tick as:
    expect(formatter?.(16000)).toBe("$16K");
    expect(formatter?.(-55000)).toBe("-$55K");

    // The identity is NOT stable: a mask flip hands Recharts a NEW formatter,
    // and that prop change is what makes `axisPropsAreEqual` miss and the
    // tick layer repaint (docs/46 §D3). Masked, the new closure prints
    // bullets — from the branch value, not from a global read.
    const before = captured.yAxes.at(-1)?.tickFormatter;
    act(() => setMasked(true));
    await waitFor(() => expect(captured.yAxes.length).toBeGreaterThan(1));
    const after = captured.yAxes.at(-1)?.tickFormatter;
    expect(after).not.toBe(before);
    expect((after as unknown as (v: number) => string)?.(16000)).toBe(MASK_TEXT);
    act(() => setMasked(false));
  });

  it("ProjectionsPanel: projected-value ticks are compact, not grouped", async () => {
    const { getProjectionDefaults, runProjections } = await import("../src/api/projections");
    vi.mocked(getProjectionDefaults).mockResolvedValue({
      start_value_cad: null,
    });
    vi.mocked(runProjections).mockResolvedValue(PROJECTION as never);

    const { container, getByRole } = render(<ProjectionsPanel />, { wrapper });
    await waitFor(() => expect(getProjectionDefaults).toHaveBeenCalled());

    fireEvent.click(getByRole("button", { name: /Run projections/ }));
    await waitFor(() => expect(moneyTicks(container).length).toBeGreaterThan(0));
    const ticks = moneyTicks(container);

    // The fixture tops out at 1,200,000 — previously `$1,200,000` on an 80px
    // axis; compact notation reads `$1.2M`.
    expect(ticks).toContain("$1.2M");
    expect(ticks).not.toContain("$1,200,000");
    expect(ticks.filter((t) => GROUPED_MILLION_TICK.test(t))).toEqual([]);

    // Same reactive-contract pin as the dividends panel: behaviour, not a
    // cross-instance identity (F-7 — moneyAxis is per-render now).
    const formatter = captured.yAxes.at(-1)?.tickFormatter as
      | ((v: number) => string)
      | undefined;
    expect(formatter).toBeDefined();
    expect(formatter?.(1200000)).toBe("$1.2M");
    expect(formatter?.(-55000)).toBe("-$55K");
  });

  it("M139: a tick index passed as arg 2 must NOT be read as a mask flag", () => {
    // Recharts calls `tickFormatter(value, index)`. An early cut of the mask
    // work gave formatAxisMoney an optional `masked?: boolean` second param —
    // so every tick except index 0 masked ITSELF (truthy index), producing a
    // dashed axis of bullets on an unmasked session. The formatter now takes
    // exactly one argument; this pins it.
    const { result } = renderHook(() => useChartChrome(), { wrapper });
    const money = result.current.moneyAxis;
    // Call it exactly as Recharts does — value plus the tick index — by
    // loosening the arity at the call site, so TS cannot paper over the bug.
    const asRecharts = money as unknown as (v: number, index: number) => string;
    for (const index of [0, 1, 2, 3]) {
      expect(asRecharts(16000, index)).toBe("$16K");
      expect(asRecharts(-55000, index)).toBe("-$55K");
    }
  });
});

describe("G7 guard — no raw `$…` tickFormatter survives in frontend/src", () => {
  it("no tickFormatter returns a template literal built from `$` + value", () => {
    const offenders: string[] = [];
    for (const file of sourceFiles("src")) {
      const text = readFileSync(file, "utf8");
      // `` `$${v}` `` — the raw money tick. Deliberately requires the SECOND
      // `$`: AccountDetail's X-axis date formatter interpolates `` `${d}T00:00:00` ``
      // and is not a money axis.
      for (const match of text.matchAll(/tickFormatter=\{[^}\n]*=>\s*`\$\$/g)) {
        const line = text.slice(0, match.index).split("\n").length;
        offenders.push(`${file}:${line}`);
      }
    }
    // formatMoney(v, account.currency) on AccountDetail's Y-axis is
    // currency-aware (USD balances) and deliberately NOT chrome.moneyAxis
    // (CAD-only) — it is not a `$`-template literal, so it is out of this grep,
    // matching the task's acceptance grep.
    expect(offenders).toEqual([]);
  });
});

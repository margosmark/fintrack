// docs/57 named tests R7–R11 + R13 — the /retirement page over the two reads it
// already owns (net-worth + cashflow). No new data sources: the fetch stub
// answers exactly the two report endpoints the page is allowed to call, and
// anything else 404s loudly.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Retirement, RETIREMENT_HONESTY, TAX_ENGINE } from "../src/pages/Retirement";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { RouteLoadError } from "../src/components/ui/RouteFallback";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";
import { SIM_DEBOUNCE_MS, runRetirementSimulation } from "../src/lib/retirementEngine";
import { CanadaTaxInputError, caTaxEngine } from "../src/lib/canadaTaxEngine";
import { flatRateTaxEngine } from "../src/lib/taxEngine";

// jsdom has no matchMedia (ThemeProvider / reduced-motion read it).
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: /min-width:\s*(\d+)px/.test(query) ? window.innerWidth >= 768 : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

// ResponsiveContainer measures the DOM, which jsdom cannot do — hand the
// chart a fixed box so the axes really draw (house shim, m109/f7).
vi.mock("recharts", async () => {
  const mod = await import("recharts");
  const ReactMod = await import("react");
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => {
      const child = Array.isArray(children) ? children[0] : children;
      if (child && typeof child === "object" && "type" in (child as object)) {
        return ReactMod.cloneElement(child as React.ReactElement, { width: 480, height: 320 });
      }
      return child;
    },
  };
});

/** Mutable server answers — each test may re-point them before rendering. */
const h = vi.hoisted(() => ({
  calls: [] as string[],
  netWorth: {
    buckets: [
      { bucket_end: "2026-11-10", cash_cad: "20000.00", positions_cad: "30000.00", net_cad: "50000.00" },
    ],
  },
  cashflow: {
    buckets: [
      { bucket: "2025-12-31", income_cad: "7000.00", expenses_cad: "4000.00", net_cad: "3000.00" },
      { bucket: "2026-01-31", income_cad: "7000.00", expenses_cad: "6000.00", net_cad: "1000.00" },
      { bucket: "2026-02-28", income_cad: "7000.00", expenses_cad: "8000.00", net_cad: "-1000.00" },
    ],
  },
}));

const DEFAULT_NET_WORTH = JSON.stringify(h.netWorth);
const DEFAULT_CASHFLOW = JSON.stringify(h.cashflow);

/** Minimal Response shape covering exactly what src/api/client.ts reads. */
function fakeResponse(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText: status === 204 ? "No Content" : "OK",
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

function handle(input: RequestInfo | URL): Response {
  const url = String(input);
  h.calls.push(url);
  const path = url.split("?")[0];
  if (path === "/api/v1/reports/net-worth") return fakeResponse(200, h.netWorth);
  if (path === "/api/v1/reports/cashflow") return fakeResponse(200, h.cashflow);
  return fakeResponse(404, { detail: `unexpected fetch: ${url}`, code: "not_found" });
}

/**
 * `underRoute` wraps the page in the route's own error boundary — the same
 * `RouteLoadError` App.tsx's `route()` mounts — so R14/R15 pin what a visitor
 * on /retirement actually sees, not just what the bare component does.
 */
function renderPage(underRoute = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  const page = underRoute ? (
    <RouteLoadError>
      <Retirement />
    </RouteLoadError>
  ) : (
    <Retirement />
  );
  return render(
    <MemoryRouter initialEntries={["/retirement"]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>{page}</ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function contributionInput(): HTMLInputElement {
  return screen.getByLabelText("Monthly contribution (CAD)") as HTMLInputElement;
}

/**
 * The withdrawal field. Matched by name, not by exact label wording, so R14
 * fails on BEHAVIOUR (route panel / clamp hint / verdict) even against the
 * pre-fix tree, where the label carried no clamp at all.
 */
function withdrawalInput(): HTMLInputElement {
  return screen.getByLabelText(/Annual withdrawal/) as HTMLInputElement;
}

beforeEach(() => {
  h.calls.length = 0;
  h.netWorth = JSON.parse(DEFAULT_NET_WORTH) as typeof h.netWorth;
  h.cashflow = JSON.parse(DEFAULT_CASHFLOW) as typeof h.cashflow;
  window.localStorage.clear();
  window.sessionStorage.clear();
  initMaskForSession(); // boot contract: unmasked, key cleared
  vi.stubGlobal("fetch", vi.fn(handle));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("R7 — the money defaults come from the data fintrack already has", () => {
  it("contribution = mean(income) − mean(expenses); withdrawal = mean(expenses) × 12", async () => {
    renderPage();
    await screen.findByTestId("retirement-success");
    // mean income 7000 − mean expenses (4000+6000+8000)/3 = 6000 → 1000.00
    expect(contributionInput().value).toBe("1000.00");
    // 6000 × 12 = 72000.00
    expect(withdrawalInput().value).toBe("72000.00");
    // …and only the two report endpoints were called — no new data source.
    expect(h.calls.every((u) => /\/api\/v1\/reports\/(net-worth|cashflow)/.test(u))).toBe(true);
    expect(h.calls.length).toBeGreaterThan(0);
  });
});

describe("R8 — the verdict and the chart render on existing data", () => {
  it("success %, the count line, the four honesty strings, the seam badge and the fan", async () => {
    const { container } = renderPage();
    const success = await screen.findByTestId("retirement-success");
    expect(success.textContent).toMatch(/^\d+\.\d%$/);
    expect(
      screen.getByText(/of 2,000 simulated paths never ran out of money before age 95\./),
    ).toBeTruthy();
    for (const line of RETIREMENT_HONESTY) {
      expect(screen.getByText(line), line).toBeTruthy();
    }
    expect(screen.getByText(/Tax engine: Canadian tax — ON/)).toBeTruthy();
    await waitFor(() => expect(container.querySelector("svg.recharts-surface")).not.toBeNull());
    expect(screen.getByRole("img", { name: /Retirement net worth/ })).toBeTruthy();
  });
});

describe("R9 — updates as you type, but committed 300 ms later", () => {
  it("the keystroke does not re-run the sim; SIM_DEBOUNCE_MS does", async () => {
    renderPage();
    const before = (await screen.findByTestId("retirement-success")).textContent;
    // The mock tenant's seeded verdict (50k portfolio, 1k/mo in, 72k/yr out,
    // seed 42) under the ca-engine's gross-up: 160 of 2000 paths survive →
    // 8.0% (the stub's net-unchanged run was 13.9% — 278 paths). Never 100,
    // never 0.
    expect(before).toBe("8.0%");

    vi.useFakeTimers();
    fireEvent.change(contributionInput(), { target: { value: "999999" } });
    // Same verdict on the keystroke — the sim still holds the old assumptions.
    expect(screen.getByTestId("retirement-success").textContent).toBe(before);

    act(() => {
      vi.advanceTimersByTime(SIM_DEBOUNCE_MS);
    });
    // 999999/mo makes every path immortal → 100.0%.
    expect(screen.getByTestId("retirement-success").textContent).toBe("100.0%");
    vi.useRealTimers();
  });
});

describe("R10 — display masking (docs/46 §D2)", () => {
  it("money figures go to bullets, percentages stay readable, inputs stay raw", async () => {
    const { container } = renderPage();
    await screen.findByTestId("retirement-success");

    const moneyCells = () =>
      Array.from(container.querySelectorAll("tbody td.tabular")).map((td) => td.textContent ?? "");
    expect(moneyCells().length).toBeGreaterThan(0);
    expect(moneyCells().some((text) => text.includes("$"))).toBe(true);

    act(() => setMasked(true));
    expect(moneyCells().every((text) => text === MASK_TEXT)).toBe(true);
    // Percentages and counts are readable (D2's percentage ruling).
    expect(screen.getByTestId("retirement-success").textContent).toMatch(/\d/);
    expect(
      screen.getByText(/simulated paths never ran out of money/).textContent ?? "",
    ).toMatch(/\d/);
    // The editable money inputs are controls, not figures — they keep their
    // raw value exactly like ProjectionsPanel's "Start value (CAD)" (docs/57 §4).
    expect(contributionInput().value).toBe("1000.00");

    act(() => setMasked(false));
    expect(moneyCells().some((text) => text.includes("$"))).toBe(true);
  });
});

describe("R11 — non-positive net worth is an empty state, never a fabricated portfolio", () => {
  it("renders the documented EmptyState and no verdict", async () => {
    h.netWorth = {
      buckets: [
        { bucket_end: "2026-11-10", cash_cad: "-2000.00", positions_cad: "-3000.00", net_cad: "-5000.00" },
      ],
    };
    renderPage();
    expect(await screen.findByTestId("empty-state")).toBeTruthy();
    expect(screen.getByText("Nothing to simulate yet")).toBeTruthy();
    expect(screen.queryByTestId("retirement-success")).toBeNull();
  });
});

describe("R13 — the page runs the Canadian engine, not the flat-rate stub (docs/57 §3)", () => {
  it("the rendered verdict matches a caTaxEngine reference run and differs from the stub's", async () => {
    renderPage();
    const rendered = (await screen.findByTestId("retirement-success")).textContent;

    // Same fixture R9 pins, re-derived here: seeded defaults (50k portfolio,
    // 1k/mo in, 72k/yr out, 5%/15%, 2000 paths, seed 42) and the page's own
    // baseYear (new Date().getFullYear() — re-read at call time, never a
    // literal, so a year rollover cannot desync this pin from the page).
    const base = {
      startingPortfolio: 50000,
      currentAge: 40,
      retirementAge: 65,
      monthlyContribution: 1000,
      annualWithdrawal: 72000,
      expectedReturnPct: 5,
      volatilityPct: 15,
      scenarios: 2000,
      seed: 42,
      baseYear: new Date().getFullYear(),
    };
    const viaCaEngine = runRetirementSimulation({ ...base, tax: caTaxEngine() }).successPct;
    const viaStub = runRetirementSimulation({ ...base, tax: flatRateTaxEngine(0) }).successPct;

    // The page's number IS the ca-engine's number…
    expect(rendered).toBe(`${viaCaEngine.toFixed(1)}%`);
    // …and the two engines really diverge on this fixture, so the first
    // assertion would fail if the page silently reverted to the stub.
    expect(viaCaEngine).not.toBe(viaStub);
  });
});

describe("R14 — an above-engine-limit withdrawal never reaches the route error panel", () => {
  it("clamps at the field's max, states the clamp, and still renders the verdict", async () => {
    renderPage(true);
    await screen.findByTestId("retirement-success");

    vi.useFakeTimers();
    fireEvent.change(withdrawalInput(), { target: { value: "78000000" } });
    act(() => {
      vi.advanceTimersByTime(SIM_DEBOUNCE_MS);
    });
    vi.useRealTimers();

    // On 178a4638 this typed value reached the engine, which refuses a
    // gross-up above $25,000,000: the CanadaTaxInputError escaped render and
    // the route answered with "the page could not be loaded, reload to fetch
    // it fresh" (security F1, t_daede909). It must not fire now.
    expect(screen.queryByTestId("route-load-error")).toBeNull();

    // The field's own validation message — the clamp, spelled out under it.
    expect(screen.getByText("Using 10000000.00")).toBeTruthy();

    // …and the verdict comes from the CLAMPED figure, not the typed text:
    // 10M/yr out of a 50k portfolio ruins every path, while the seeded
    // 72k fixture is 8.0% (R9) — so 0.0% can only be the clamped run.
    const clamped = runRetirementSimulation({
      startingPortfolio: 50000,
      currentAge: 40,
      retirementAge: 65,
      monthlyContribution: 1000,
      annualWithdrawal: 10_000_000,
      expectedReturnPct: 5,
      volatilityPct: 15,
      scenarios: 2000,
      seed: 42,
      baseYear: new Date().getFullYear(),
      tax: caTaxEngine(),
    });
    expect(clamped.successPct).toBe(0);
    expect(screen.getByTestId("retirement-success").textContent).toBe(
      `${clamped.successPct.toFixed(1)}%`,
    );
  });
});

describe("R15 — the tax engine's own error class never escapes render", () => {
  it("an engine refusal degrades to the no-plan message, not RouteLoadError", async () => {
    renderPage(true);
    await screen.findByTestId("retirement-success");

    // The R14 clamp keeps the REAL engine's $25M refusal out of reach, so
    // this pin gives the page's own seam instance an engine that refuses the
    // withdrawal: a genuine CanadaTaxInputError — the class the pre-fix
    // `instanceof RetirementInputError` check missed (t_daede909).
    const refusal = vi
      .spyOn(TAX_ENGINE, "grossWithdrawalFor")
      .mockImplementation(() => {
        throw new CanadaTaxInputError(
          "input_too_large",
          "gross-up for netSpend 200000 exceeds the engine limit",
        );
      });
    try {
      vi.useFakeTimers();
      fireEvent.change(withdrawalInput(), { target: { value: "200000" } });
      act(() => {
        vi.advanceTimersByTime(SIM_DEBOUNCE_MS);
      });
      vi.useRealTimers();

      expect(refusal).toHaveBeenCalled();
      expect(screen.queryByTestId("route-load-error")).toBeNull();
      // The documented no-plan path (docs/57 §4 copy 6): say it, never
      // crash, and never fabricate a verdict from inputs the engine refused.
      expect(screen.getByTestId("retirement-input-issue")).toBeTruthy();
      expect(
        screen.getByText(
          "These assumptions are outside the simulator's supported range — adjust them in the Assumptions panel and the plan will re-run.",
        ),
      ).toBeTruthy();
      expect(screen.queryByTestId("retirement-success")).toBeNull();
    } finally {
      refusal.mockRestore();
    }
  });
});

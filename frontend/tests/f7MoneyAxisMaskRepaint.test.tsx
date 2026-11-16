// F-7 (P2, Ledger security review t_ac65f44a) — the TEN chart money axes
// beyond AccountValueChart must live-repaint on a display-mask flip, in BOTH
// directions, on ONE mount, with zero network traffic and no reload/remount.
// (TEN since M188 added pages/Retirement.tsx; the census below still records
// the original nine-member measurement.)
//
// The class, per the finding: these call sites pass `chrome.moneyAxis` as
// recharts' `tickFormatter`, and `useChartChrome()` handed recharts the SAME
// stable module function (`formatAxisMoney`) on every render — so a mask flip
// changed no axis prop, `axisPropsAreEqual` bailed, and the tick layer kept
// painting the mount state while every other money string on the page flipped.
// `formatAxisMoney` itself IS mask-aware, but a formatter that is never
// re-invoked never gets to say so.
//
// The fix under test (docs/46 §D3): `useChartChrome()` now subscribes to the
// mask itself and builds `moneyAxis` as a FRESH closure per render, so the
// prop identity tracks the mask — the reactive contract the B1 blocker fix
// introduced for AccountValueChart, centralised for every consumer of the
// hook. Each test here pins the OBSERVABLE behaviour the finding demands, not
// the implementation: read the tick strings out of the SVG the axis actually
// drew, flip, read again, on the same axis node, with the fetch log frozen.
//
// MEASURED on the pre-fix tree (42c9ac7 + blocker 6d4e445), same file, same
// run: 3 of 9 RED — Forecast (never re-rendered: no subscription AND a
// `useMemo`ed `chartRows`), DividendsPanel (subscribed, but `data.by_month`
// keeps its query-cache identity so no axis prop changed), and
// UnrealizedBarChart (frozen on the unmask direction). The other six passed
// ONLY because their `data` array is rebuilt every render, which churns
// recharts' store — the accidental repaint docs/46 §D3 warns about, not a
// contract. After the fix all nine repaint by prop identity, memoized data or
// not — and the tenth (Retirement, M188) never had a pre-fix state to
// measure, it joins the contract directly.
//
// jsdom has no layout, so ResponsiveContainer measures 0x0 — the house stub
// (tests/m140ChartAxis.test.tsx) hands every chart a fixed box.

import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../src/theme/ThemeProvider";
import { BillHistoryBarChart } from "../src/components/charts/BillHistoryBarChart";
import { CashflowComposedChart } from "../src/components/charts/CashflowComposedChart";
import { NetWorthAreaChart } from "../src/components/charts/NetWorthAreaChart";
import { PriceHistoryChart } from "../src/components/charts/PriceHistoryChart";
import { SpendingStackedBarChart } from "../src/components/charts/SpendingStackedBarChart";
import { UnrealizedBarChart } from "../src/components/charts/UnrealizedBarChart";
import { DividendsPanel } from "../src/components/reports/DividendsPanel";
import { ProjectionsPanel } from "../src/components/reports/ProjectionsPanel";
import { Forecast } from "../src/pages/Forecast";
import { Retirement } from "../src/pages/Retirement";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";

// --- harness ----------------------------------------------------------------

// Recharts' ResponsiveContainer measures the DOM, which jsdom cannot do; hand
// every chart a fixed box so the axes really draw ticks.
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

// ProjectionsPanel reads the identity through useAuth; there is no AuthProvider
// in this harness, and the panel's own logic treats a null user as the normal
// owner tenant (same stub tests/reportMoneyAxes.test.tsx runs with).
vi.mock("../src/auth/AuthContext", () => ({ useAuth: () => ({ user: null }) }));

// Forecast and ProjectionsPanel announce through the toast hook; the messages
// are incidental to every assertion here (m109's stub).
vi.mock("../src/components/ui/Toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/components/ui/Toast")>();
  return {
    ...actual,
    useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  };
});

// jsdom has no matchMedia (ThemeProvider / reduced-motion read it).
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

/** Every network call the client made — the flip window must add none. */
const h = vi.hoisted(() => ({ calls: [] as Array<{ url: string; method: string }> }));

const FORECAST_DEFAULTS = {
  as_of: "2026-10-06",
  reporting_currency: "CAD",
  net_worth_cad: "80235.79",
  cash_cad: "12345.67",
  positions_cad: "67890.12",
  income_monthly_cad: "5000.00",
  expense_monthly_cad: "3800.00",
  flow_window_months: 12,
  recurring_rows: 7,
  scenario_count: 0,
};

const FORECAST_BUCKETS = ["2026-11-06", "2027-11-06", "2028-10-06"];

function forecastRunPayload(): unknown {
  return {
    as_of: FORECAST_DEFAULTS.as_of,
    horizon_months: 24,
    granularity: "month",
    inflation_pct: "2.00",
    annual_return_pct: "0.00",
    flow_window_months: 12,
    reporting_currency: "CAD",
    buckets: FORECAST_BUCKETS,
    inputs: {
      cash_cad: "12345.67",
      positions_cad: "67890.12",
      net_worth_cad: "80235.79",
      history_from: "2025-10-07",
      history_to: "2026-10-06",
      income_monthly_cad: "5000.00",
      expense_monthly_cad: "3800.00",
      recurring_income_monthly_cad: "0.00",
      recurring_expense_monthly_cad: "450.00",
      residual_income_monthly_cad: "5000.00",
      residual_expense_monthly_cad: "3350.00",
      recurring_rows: 7,
    },
    baseline: {
      id: "baseline",
      name: "Baseline",
      values: ["100000.00", "110000.00", "120000.00"],
      real_values: ["99000.00", "107000.00", "115000.00"],
      final_cad: "120000.00",
      delta_cad: "0.00",
    },
    scenarios: [],
    disclaimer: "Projection, not a prediction.",
  };
}

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

function projectionSeries(values: number[]) {
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
      series: projectionSeries([0, 300000, 600000, 900000, 1200000]),
      final_value_cad: "1200000.00",
      total_contributed_cad: "0.00",
      total_gain_cad: "1200000.00",
    },
  ],
};

/** Minimal Response shape covering exactly what src/api/client.ts reads. */
function fakeResponse(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText: "OK",
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

function handle(input: RequestInfo | URL, init?: RequestInit): Response {
  const url = String(input);
  const method = (init?.method ?? "GET").toUpperCase();
  h.calls.push({ url, method });

  const path = url.split("?")[0];
  if (path === "/api/v1/forecast/defaults") return fakeResponse(200, FORECAST_DEFAULTS);
  if (path === "/api/v1/forecast/run") return fakeResponse(200, forecastRunPayload());
  if (path === "/api/v1/forecast/scenarios") {
    if (method === "POST") return fakeResponse(201, { id: "sc_x", name: "Scenario", events: [] });
    return fakeResponse(200, { items: [], count: 0 });
  }
  if (path === "/api/v1/reports/dividends") return fakeResponse(200, DIVIDENDS);
  if (path === "/api/v1/reports/net-worth")
    return fakeResponse(200, { buckets: NET_WORTH_BUCKETS });
  if (path === "/api/v1/reports/cashflow")
    return fakeResponse(200, { buckets: CASHFLOW_BUCKETS });
  if (path === "/api/v1/projections/defaults") return fakeResponse(200, { start_value_cad: null });
  if (path === "/api/v1/projections/run") return fakeResponse(200, PROJECTION);
  return fakeResponse(404, { detail: "Not found", code: "not_found" });
}

function client(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

const Y_AXIS = ".recharts-yAxis-tick-labels";
const X_AXIS = ".recharts-xAxis-tick-labels";

function moneyTicks(container: HTMLElement, axis: string): string[] {
  return [...container.querySelectorAll<SVGTextElement>(`${axis} text`)].map(
    (t) => t.textContent ?? "",
  );
}

/**
 * The acceptance itself, once per chart: dollars → bullets → dollars → bullets
 * on ONE mount, the SAME axis node throughout, and a frozen network log.
 */
async function expectLiveMaskRepaint(container: HTMLElement, axis: string): Promise<void> {
  await waitFor(() => expect(moneyTicks(container, axis).length).toBeGreaterThan(0));

  const axisNode = container.querySelector(axis);
  expect(axisNode).not.toBeNull();

  const dollars = moneyTicks(container, axis);
  expect(dollars.some((t) => t.includes("$"))).toBe(true);
  expect(dollars.every((t) => t !== MASK_TEXT)).toBe(true);

  const networkAtFlip = h.calls.length;

  act(() => setMasked(true));
  await waitFor(() =>
    expect(moneyTicks(container, axis).every((t) => t === MASK_TEXT)).toBe(true),
  );

  act(() => setMasked(false));
  await waitFor(() => expect(moneyTicks(container, axis)).toEqual(dollars));

  act(() => setMasked(true));
  await waitFor(() =>
    expect(moneyTicks(container, axis).every((t) => t === MASK_TEXT)).toBe(true),
  );

  // Same axis element every time — the ticks REDREW; nothing remounted.
  expect(container.querySelector(axis)).toBe(axisNode);
  // A mask flip is a render-time transform: nothing may go over the wire, in
  // either direction, for the whole window.
  expect(h.calls.length).toBe(networkAtFlip);
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.setItem("theme", "light");
  initMaskForSession(); // boot contract: unmasked, key cleared
  h.calls.length = 0;
  vi.stubGlobal("fetch", vi.fn(handle));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Three buckets so the axis draws more than one tick. */
const NET_WORTH_BUCKETS = [
  { bucket_end: "2026-01-31", cash_cad: "400.00", positions_cad: "600.00", net_cad: "1000.00" },
  { bucket_end: "2026-02-28", cash_cad: "400.00", positions_cad: "800.00", net_cad: "1200.00" },
  { bucket_end: "2026-03-31", cash_cad: "400.00", positions_cad: "1000.00", net_cad: "1400.00" },
] as never;

const CASHFLOW_BUCKETS = [
  { bucket: "2026-01-31", income_cad: "5200.00", expenses_cad: "3800.00", net_cad: "1400.00" },
  { bucket: "2026-02-28", income_cad: "5400.00", expenses_cad: "3600.00", net_cad: "1800.00" },
  { bucket: "2026-03-31", income_cad: "5600.00", expenses_cad: "4100.00", net_cad: "1500.00" },
] as never;

const SPEND_SLICES = [
  { id: 1, name: "Groceries", bucket: "2026-01-31", total_cad: "620.00" },
  { id: 1, name: "Groceries", bucket: "2026-02-28", total_cad: "740.00" },
  { id: 2, name: "Rent", bucket: "2026-01-31", total_cad: "2400.00" },
  { id: 2, name: "Rent", bucket: "2026-02-28", total_cad: "2400.00" },
] as never;

const BILL_POINTS = [
  {
    period: "2026-01-31",
    label: "Jan 2026",
    total: "1450.00",
    missed: false,
    count: 3,
    rolling_six: "1400.00",
  },
  {
    period: "2026-02-28",
    label: "Feb 2026",
    total: "1520.00",
    missed: false,
    count: 4,
    rolling_six: "1440.00",
  },
  {
    period: "2026-03-31",
    label: "Mar 2026",
    total: "1380.00",
    missed: false,
    count: 3,
    rolling_six: "1450.00",
  },
] as never;

const PERFORMANCE_ITEMS = [
  {
    security_id: 1,
    symbol: "XEQT",
    quantity: "100",
    realized_cad: "0.00",
    unrealized_cad: "25000.00",
    acb_pool_cad: "10000.00",
    market_value_cad: "65000.00",
  },
  {
    security_id: 2,
    symbol: "VFV",
    quantity: "50",
    realized_cad: "0.00",
    unrealized_cad: "-8000.00",
    acb_pool_cad: "12000.00",
    market_value_cad: "30000.00",
  },
] as never;

const PRICE_HISTORY = [
  { date: "2026-07-01", price: "100.00" },
  { date: "2026-08-01", price: "120.00" },
  { date: "2026-09-01", price: "140.00" },
  { date: "2026-10-01", price: "160.00" },
];

describe("F-7 — the ten non-AccountValueChart money axes repaint on a mask flip", () => {
  it("NetWorthAreaChart (charts/NetWorthAreaChart.tsx:98)", async () => {
    const { container } = render(
      <ThemeProvider>
        <NetWorthAreaChart buckets={NET_WORTH_BUCKETS} granularity="month" />
      </ThemeProvider>,
    );
    await expectLiveMaskRepaint(container, Y_AXIS);
  });

  it("CashflowComposedChart (charts/CashflowComposedChart.tsx:82)", async () => {
    const { container } = render(
      <ThemeProvider>
        <CashflowComposedChart buckets={CASHFLOW_BUCKETS} granularity="month" />
      </ThemeProvider>,
    );
    await expectLiveMaskRepaint(container, Y_AXIS);
  });

  it("SpendingStackedBarChart (charts/SpendingStackedBarChart.tsx:108)", async () => {
    const { container } = render(
      <ThemeProvider>
        <SpendingStackedBarChart items={SPEND_SLICES} granularity="month" />
      </ThemeProvider>,
    );
    await expectLiveMaskRepaint(container, Y_AXIS);
  });

  it("BillHistoryBarChart (charts/BillHistoryBarChart.tsx:142)", async () => {
    const { container } = render(
      <ThemeProvider>
        <BillHistoryBarChart points={BILL_POINTS} granularity="month" />
      </ThemeProvider>,
    );
    await expectLiveMaskRepaint(container, Y_AXIS);
  });

  it("UnrealizedBarChart (charts/UnrealizedBarChart.tsx:57, the money X-axis)", async () => {
    const { container } = render(
      <ThemeProvider>
        <UnrealizedBarChart items={PERFORMANCE_ITEMS} />
      </ThemeProvider>,
    );
    await expectLiveMaskRepaint(container, X_AXIS);
  });

  it("PriceHistoryChart (charts/PriceHistoryChart.tsx:100)", async () => {
    const { container } = render(
      <ThemeProvider>
        <PriceHistoryChart history={PRICE_HISTORY} symbol="XEQT" granularity="day" />
      </ThemeProvider>,
    );
    await expectLiveMaskRepaint(container, Y_AXIS);
  });

  it("Forecast (pages/Forecast.tsx:612)", async () => {
    render(
      <MemoryRouter initialEntries={["/forecast"]}>
        <QueryClientProvider client={client()}>
          <ThemeProvider>
            <Forecast />
          </ThemeProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
    // The chart only mounts once the run answers — readiness first, then the
    // flip window opens with a stable network log.
    await waitFor(() => expect(document.querySelector(Y_AXIS)).not.toBeNull());
    const container = document.body as HTMLElement;
    await expectLiveMaskRepaint(container, Y_AXIS);
  });

  it("DividendsPanel (reports/DividendsPanel.tsx:174)", async () => {
    const { container } = render(
      <QueryClientProvider client={client()}>
        <ThemeProvider>
          <DividendsPanel />
        </ThemeProvider>
      </QueryClientProvider>,
    );
    await expectLiveMaskRepaint(container, Y_AXIS);
  });

  it("Retirement (pages/Retirement.tsx:600, the M188 tenth member)", async () => {
    render(
      <MemoryRouter initialEntries={["/retirement"]}>
        <QueryClientProvider client={client()}>
          <ThemeProvider>
            <Retirement />
          </ThemeProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
    // Readiness: the verdict gate keeps the skeleton up until the seeded
    // defaults commit, so wait for the axis the fan chart actually draws.
    await waitFor(() => expect(document.querySelector(Y_AXIS)).not.toBeNull());
    const container = document.body as HTMLElement;
    await expectLiveMaskRepaint(container, Y_AXIS);
  });
  it("ProjectionsPanel (reports/ProjectionsPanel.tsx:371)", async () => {
    const { container, getByRole, findByText } = render(
      <MemoryRouter initialEntries={["/reports"]}>
        <QueryClientProvider client={client()}>
          <ThemeProvider>
            <ProjectionsPanel />
          </ThemeProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
    // The chart exists only after a run; the panel fetches its defaults first.
    await findByText(/Start value \(CAD\)/);
    fireEvent.click(getByRole("button", { name: /Run projections/ }));
    await expectLiveMaskRepaint(container, Y_AXIS);
  });
});

// F-11 (P2, security final signature t_a2616dd5) — two (really three) surfaces
// on `/forecast` printed RAW absolutes while the display mask was ON:
//
//   * the "End value per series" table (Forecast.tsx) — baseline end value,
//     baseline delta, scenario end value, scenario delta: the response's own
//     strings, spliced in with no mask routing, while the chart's y-axis
//     directly above rendered ••••• (F-7 fixed the axis, not the table);
//   * the Assumptions list (lib/forecastCopy.ts `buildAssumptions`) —
//     `Net worth (CAD): 231305.38`, `Cash … 482.12`, `Positions … 230823.26`,
//     `Income … = recurring … + residual …`, `Expense … = recurring … +
//     residual …`. forecastCopy.ts had ZERO mask references;
//   * the scenario EVENT rows (same file) — `House purchase 2030-01-01 —
//     600000.00 price, 120000.00 down` — same class, found while fixing the
//     first two: the demo tenant seeds no forecast scenarios, so the security
//     sweep never rendered a row.
//
// Why a behavioural pin: `scripts/sweep_money.mjs` tracks CALLS to the shared
// mask-aware formatters, and none of these surfaces call one — they print
// strings the server composed. The sweep can prove component SUBSCRIPTION and
// can never see this class (docs/46 §D4's same reasoning for the F-1 banner).
// So, exactly like `tests/dashboardBudgetBannerMask.test.tsx`, this mounts the
// REAL <Forecast/> inside the REAL <AppShell/>, feeds it the server's own run
// payload and reads the DOM back — text AND aria-labels, because a chart's
// text alternative IS its content for assistive tech (docs/46 §D2, F-9).
//
//   1. masked   — no `\d{1,3}(,\d{3})*\.\d{2}` anywhere on /forecast;
//   2. control  — the same page unmasked prints the real figures, so (1) can
//                 never pass on an empty/unrendered page;
//   3. a flip of the shipped chip re-masks and re-unmasks IN PLACE, same DOM
//      nodes, zero network requests (docs/46 §D3/D5).
//
// Percentages (`Inflation: 2.00%`), ISO dates and row counts are parked by the
// scanner exactly the way `maskAmountsInText` parks them: docs/46 §D2 rules
// proportional values non-sensitive, so they must survive both directions.
//
// House matcher style: `.getAttribute(...)`, no jest-dom.
import type { ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../src/components/layout/AppShell";
import { Forecast } from "../src/pages/Forecast";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";
import { ThemeProvider } from "../src/theme/ThemeProvider";

const h = vi.hoisted(() => ({
  calls: [] as Array<{ url: string; method: string; body: unknown }>,
  scenarios: [] as Array<{ id: string; name: string; events: unknown[] }>,
}));

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
  scenario_count: 1,
};

/** The five absolute figures the finding measured leaking (docs/45 §1.2). */
const RUN_INPUTS = {
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
};

const BUCKETS = ["2026-11-06", "2027-11-06", "2028-10-06"];

/** The two dollar readings deliberately DIVERGE so the toggle is observable. */
function runPayload(url: URL) {
  const ids = (url.searchParams.get("scenario_ids") ?? "").split(",").filter(Boolean);
  return {
    as_of: FORECAST_DEFAULTS.as_of,
    horizon_months: Number(url.searchParams.get("horizon_months")),
    granularity: url.searchParams.get("granularity") ?? "day",
    inflation_pct: url.searchParams.get("inflation_pct") ?? "2.00",
    annual_return_pct: url.searchParams.get("annual_return_pct") ?? "0.00",
    flow_window_months: Number(url.searchParams.get("flow_window_months")),
    reporting_currency: "CAD",
    buckets: BUCKETS,
    inputs: RUN_INPUTS,
    baseline: {
      id: "baseline",
      name: "Baseline",
      values: ["100000.00", "110000.00", "120000.00"],
      real_values: ["99000.00", "107000.00", "115000.00"],
      final_cad: "120000.00",
      delta_cad: "0.00",
    },
    scenarios: ids.map((id) => ({
      id,
      name: h.scenarios.find((entry) => entry.id === id)?.name ?? id,
      values: ["105000.00", "115000.00", "125000.00"],
      real_values: ["104000.00", "112000.00", "119000.00"],
      final_cad: "125000.00",
      delta_cad: "5000.00",
    })),
    disclaimer: "Projection, not a prediction.",
  };
}

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

function handle(input: RequestInfo | URL, init?: RequestInit): Response {
  const url = String(input);
  const method = (init?.method ?? "GET").toUpperCase();
  const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
  h.calls.push({ url, method, body });

  const path = url.split("?")[0];
  if (path === "/api/v1/forecast/defaults") return fakeResponse(200, FORECAST_DEFAULTS);
  if (path === "/api/v1/forecast/scenarios") {
    if (method === "POST") {
      const created = body as { id: string; name: string; events: unknown[] };
      h.scenarios.push(created);
      return fakeResponse(201, created);
    }
    return fakeResponse(200, { items: h.scenarios, count: h.scenarios.length });
  }
  if (path === "/api/v1/forecast/run") {
    return fakeResponse(200, runPayload(new URL(url, "http://test.local")));
  }
  return fakeResponse(404, { detail: "Not found", code: "not_found" });
}

// --- harness ----------------------------------------------------------------

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listCategories: vi.fn(),
}));

vi.mock("../src/api/accounts", () => ({ listAccounts: mocks.listAccounts }));
vi.mock("../src/api/categories", () => ({ listCategories: mocks.listCategories }));
vi.mock("../src/api/notifications", () => ({
  INBOX_QUERY_KEY: ["notifications", "inbox"],
  getNotificationInbox: vi.fn().mockResolvedValue({ items: [], unread_count: 0 }),
  markNotificationRead: vi.fn().mockResolvedValue(undefined),
  markAllNotificationsRead: vi.fn().mockResolvedValue(undefined),
  unreadBadgeLabel: () => "",
}));
// The real AuthProvider calls `initMaskForSession()` when the identity
// resolves, which would silently re-mask mid-assertion (tests/m139MaskChip
// and the F-1 pin both mock the context for exactly this reason).
vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({
    user: {
      id: "f11-user",
      email: "f11@example.com",
      privacy_mode: false,
      currency_tracker_enabled: false,
      dashboard_preset: "overview",
    },
    setUser: vi.fn(),
    logout: vi.fn(),
  }),
  isReadOnlySession: () => false,
}));
// Toasts are incidental to every assertion here — a success message must never
// be mistaken for the table row it announces (m109 shape).
vi.mock("../src/components/ui/Toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/components/ui/Toast")>();
  return {
    ...actual,
    useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  };
});
vi.mock("../src/api/projections", () => ({
  getProjectionDefaults: vi.fn().mockResolvedValue({ start_value_cad: null }),
  runProjections: vi.fn(),
}));
vi.mock("../src/lib/projectionEngine", () => ({
  ProjectionInputError: class ProjectionInputError extends Error {},
  runProjectionEngine: vi.fn(),
}));

// jsdom has no matchMedia; ThemeProvider, the shell's 768px breakpoint and
// framer-motion's reduced-motion check all ask for it. Query-aware like
// tests/dashboardBudgetBannerMask.test.tsx so the desktop rail (and with it
// the mask chip) stays rendered.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: /min-width/.test(query),
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

// ResponsiveContainer measures the DOM, which jsdom cannot do — hand the
// chart a fixed box so the axes render (m109 / f9 shim).
vi.mock("recharts", async () => {
  const mod = await import("recharts");
  const ReactMod = await import("react");
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => {
      const child = Array.isArray(children) ? children[0] : children;
      if (child && typeof child === "object" && "type" in (child as object)) {
        return ReactMod.cloneElement(child as React.ReactElement, { width: 480, height: 340 });
      }
      return child;
    },
  };
});

/** Stand-in EventSource so the shell's price stream never touches jsdom. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  constructor(url: string) {
    FakeEventSource.instances.push(this);
    this.url = url;
  }
  close(): void {}
}

/** The real shell over the real route — the shape the finding measured. */
function renderRouted(entry: string, ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const tree = (
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route element={<AppShell />}>
              <Route path="*" element={ui} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  );
  return { ...render(tree), client, tree };
}

/**
 * Money-shaped text still on screen — `120000.00`, `1,240.00`, `$100,000.00`:
 * the shapes a shoulder-surfer reads as dollars. Scans textContent AND every
 * `aria-label`, because a chart's text alternative is the chart for assistive
 * tech (docs/46 §D2 / F-9) and the end-value table region is itself labelled.
 *
 * ISO dates park first, then percentages — parked EXACTLY the way
 * `maskAmountsInText` parks them: docs/46 §D2 rules proportional values
 * non-sensitive, so `Inflation: 2.00%` must survive a mask.
 */
function moneyLeaks(root: HTMLElement): string[] {
  const parts = [root.textContent ?? ""];
  for (const el of root.querySelectorAll("[aria-label]")) {
    parts.push(el.getAttribute("aria-label") ?? "");
  }
  const parked = parts
    .join("\n")
    .replace(/\d{4}-\d{2}-\d{2}/g, " ")
    .replace(/\d+(?:\.\d+)?\s?%/g, " ");
  return parked.match(/\d{1,3}(?:,\d{3})*\.\d{2}/g) ?? [];
}

/** The end-value table region, addressed by its own accessible name. */
function endValueTable(): HTMLElement {
  const region = screen.getByRole("region", { name: "End value per series" });
  if (!(region instanceof HTMLElement)) throw new Error("end-value region missing");
  return region;
}

/** Open the Assumptions panel (collapsed by default) and return its list. */
async function openAssumptions(): Promise<HTMLElement> {
  const toggle = screen.getByRole("button", { name: "Assumptions" });
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));
  const panel = document.getElementById("forecast-assumptions");
  if (!(panel instanceof HTMLElement)) throw new Error("assumptions panel missing");
  return panel;
}

/** Include the seeded scenario so the run carries a scenario row (its own
 *  end value + delta are half the finding). Re-runs the query — that fetch is
 *  part of SETUP, never part of the flip count. */
async function includeScenario(): Promise<void> {
  fireEvent.click(await screen.findByRole("checkbox", { name: "Overlay House in 2030" }));
  // The run re-fires with scenario_ids=sc_house; wait on the SERIES label,
  // never on the end value — that cell is ••••• under a mask (F-11), so an
  // unmasked literal here would make the masked test time out on its own fix.
  await waitFor(() =>
    expect(screen.getByRole("region", { name: "End value per series" }).textContent).toContain(
      "House in 2030",
    ),
  );
}

/** The scenario event row's copy. Two subtrees mention the event — the
 *  horizon heads-up ("After 2028-10-06: House in 2030 — House purchase
 *  2030-01-01", no amounts) and the row itself — so filter on the row's own
 *  shape (`… — <amount> price, <amount> down`, bullets when masked), then take
 *  the deepest match: `getAllByText` matches the `<li>` AND the `<span>` inside
 *  it (an ancestor's textContent includes its children's). This is the node a
 *  flip must repaint without remounting. */
async function eventRow(): Promise<HTMLElement> {
  const list = (): HTMLElement[] =>
    screen
      .getAllByText(/House purchase 2030-01-01/)
      .filter((el: HTMLElement) => / price, /.test(el.textContent ?? ""));
  await waitFor(() => expect(list().length).toBeGreaterThan(0));
  const deepest = list().reduce((a, b) => (a.contains(b) ? b : a));
  if (!(deepest instanceof HTMLElement)) throw new Error("event row missing");
  return deepest;
}

beforeEach(() => {
  h.calls.length = 0;
  h.scenarios.length = 0;
  h.scenarios.push({
    id: "sc_house",
    name: "House in 2030",
    events: [
      {
        id: "ev_house",
        type: "house_purchase",
        date: "2030-01-01",
        home_price_cad: "600000.00",
        down_payment_cad: "120000.00",
        mortgage_rate_pct: "5.00",
        amortization_years: 25,
      },
    ],
  });
  sessionStorage.clear();
  initMaskForSession();
  localStorage.clear();
  localStorage.setItem("theme", "light");
  localStorage.setItem("fin_tracker.tour.v1.f11-user", "completed");
  mocks.listAccounts.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  mocks.listCategories.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  vi.stubGlobal("fetch", vi.fn(handle));
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  // Wrapped: vitest runs afterEach LIFO, so this fires BEFORE setup.ts's RTL
  // cleanup — a tree from the previous test is still mounted, and
  // `initMaskForSession()` notifies its mask subscribers.
  vi.unstubAllGlobals();
  sessionStorage.clear();
  initMaskForSession();
});

describe("F-11 — /forecast never prints raw money while the mask is on", () => {
  it("masked: the end-value table, the assumptions figures and every aria-label carry no money", async () => {
    setMasked(true);
    const { container } = renderRouted("/forecast", <Forecast />);
    await screen.findByRole("img", { name: /Net worth forecast/ });
    await includeScenario();

    // 1. The table — four absolute figures, all bullets.
    const table = endValueTable();
    await waitFor(() => expect(table.textContent).toContain(MASK_TEXT));
    expect(moneyLeaks(table)).toEqual([]);
    expect((table.textContent?.match(/•••••/g) ?? []).length).toBe(4);
    // The series LABELS still read — the shape of the table survives.
    expect(table.textContent).toContain("Baseline");
    expect(table.textContent).toContain("House in 2030");

    // 2. The assumptions list — the five absolute figures mask, the
    //    percentages, the row count and the history dates do not (§D2).
    const panel = await openAssumptions();
    expect(panel.textContent).toContain(`Net worth (CAD): ${MASK_TEXT}`);
    expect(panel.textContent).toContain(`Cash (CAD): ${MASK_TEXT}`);
    expect(panel.textContent).toContain(`Positions (CAD): ${MASK_TEXT}`);
    expect(panel.textContent).toContain(
      `Income (CAD): ${MASK_TEXT} = recurring ${MASK_TEXT} + residual ${MASK_TEXT}`,
    );
    expect(panel.textContent).toContain(
      `Expense (CAD): ${MASK_TEXT} = recurring ${MASK_TEXT} + residual ${MASK_TEXT}`,
    );
    expect(panel.textContent).toContain("Inflation: 2.00%");
    expect(panel.textContent).toContain("Recurring rows in the history window: 7");
    expect(moneyLeaks(panel)).toEqual([]);

    // 3. The scenario event row — same class, same page.
    const row = await eventRow();
    expect(row.textContent).toContain(MASK_TEXT);
    expect(row.textContent).not.toContain("600000.00");
    expect(row.textContent).toContain("2030-01-01");

    // 4. Whole surface — text AND aria-labels (the chart's text alternative
    //    is where F-9 lived).
    expect(moneyLeaks(container)).toEqual([]);
  });

  it("control: unmasked, the same page prints the server's own figures verbatim", async () => {
    const { container } = renderRouted("/forecast", <Forecast />);
    await screen.findByRole("img", { name: /Net worth forecast/ });
    await includeScenario();

    // The end-value table: the response's own strings (docs/45 §Boundary).
    const table = endValueTable();
    expect(table.textContent).toContain("120000.00");
    expect(table.textContent).toContain("125000.00");
    expect(table.textContent).toContain("5000.00");
    expect(table.textContent).not.toContain(MASK_TEXT);

    // The assumptions list: all five absolute figures, verbatim.
    const panel = await openAssumptions();
    expect(panel.textContent).toContain("Net worth (CAD): 80235.79");
    expect(panel.textContent).toContain("Cash (CAD): 12345.67");
    expect(panel.textContent).toContain("Positions (CAD): 67890.12");
    expect(panel.textContent).toContain("5000.00 = recurring 0.00 + residual 5000.00");
    expect(panel.textContent).toContain("3800.00 = recurring 450.00 + residual 3350.00");

    // The scenario event row.
    const row = await eventRow();
    expect(row.textContent).toContain("600000.00 price, 120000.00 down");

    // The scanner's inverse — it FINDS the figures when the mask is off, so
    // the `[]` above is a real result and not an empty-page artifact. Note the
    // finding's regex has a THREE-digit window (`\d{1,3}(,\d{3})*`), so a bare
    // `80235.79` is reported as its `235.79` tail — that is the regex the
    // acceptance is written around, and the full strings are asserted on the
    // surfaces above.
    const leaks = moneyLeaks(container);
    expect(leaks.length).toBeGreaterThan(0);
    expect(leaks).toContain("100,000.00"); // the comma shape (chart label)
    expect(leaks).toContain("235.79"); // …and the bare shape (assumptions)
    expect(leaks).toContain("345.67");
  });

  it("a flip of the shipped chip re-masks and re-unmasks IN PLACE, with zero network requests", async () => {
    const { container } = renderRouted("/forecast", <Forecast />);
    await screen.findByRole("img", { name: /Net worth forecast/ });
    await includeScenario();

    const table = endValueTable();
    const panel = await openAssumptions();
    const chart = screen.getByRole("img", { name: /Net worth forecast/ });
    const row = await eventRow();

    // Setup done — the mask flip must not touch the wire (docs/46 §D5).
    const before = h.calls.length;
    expect(table.textContent).toContain("120000.00");

    fireEvent.click(screen.getByTestId("mask-chip"));
    await waitFor(() =>
      expect(screen.getByTestId("mask-chip").getAttribute("aria-pressed")).toBe("true"),
    );

    // Same DOM nodes everywhere — a flip re-renders, never remounts (D3/D6).
    await waitFor(() => expect(table.textContent).not.toMatch(/\d+\.\d\d/));
    expect(endValueTable()).toBe(table);
    expect(table.textContent).toContain(MASK_TEXT);
    expect(moneyLeaks(table)).toEqual([]);

    expect(screen.getByRole("button", { name: "Assumptions" })).toBeTruthy();
    expect(panel.textContent).toContain(`Net worth (CAD): ${MASK_TEXT}`);
    expect(moneyLeaks(panel)).toEqual([]);

    // The chart's TEXT ALTERNATIVE flips too — it is money built inside
    // `chartAriaLabel`'s facts, and a memoized label would freeze it at the
    // mount state (the D6 rule: no useMemo around money).
    await waitFor(() =>
      expect(chart.getAttribute("aria-label") ?? "").not.toMatch(/\d{1,3}(?:,\d{3})*\.\d{2}/),
    );
    expect(row.textContent).toContain(MASK_TEXT);
    expect(moneyLeaks(container)).toEqual([]);

    expect(h.calls.length).toBe(before);

    // Back the other way, same nodes, still nothing on the wire.
    fireEvent.click(screen.getByTestId("mask-chip"));
    await waitFor(() => expect(table.textContent).toContain("120000.00"));
    expect(endValueTable()).toBe(table);
    expect(panel.textContent).toContain("Net worth (CAD): 80235.79");
    expect(chart.getAttribute("aria-label") ?? "").toContain("$100,000.00");
    expect(row.textContent).toContain("600000.00 price, 120000.00 down");
    expect(h.calls.length).toBe(before);
  });
});

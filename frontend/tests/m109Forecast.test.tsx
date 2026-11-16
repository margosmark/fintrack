// M109 — the /forecast surface (docs/45). The four NAMED tests:
//
//   F15  all four BOUNDARY strings render on the page. They are load-bearing
//        copy: the spec pins them verbatim, so they are asserted here as
//        literals (against the spec, not against forecastCopy.ts — a drift in
//        the source must fail this test, not travel with it).
//   F16  the future/today dollar toggle switches the plotted series WITHOUT
//        a refetch: both readings arrive in one run response, so the fetch
//        count must not move and the exact strings on screen must.
//   F17  the rail exposes a "Forecast" row, and the Reports projection card
//        exposes "Open what-if scenarios".
//   F18  scenario create → copy → delete round-trips through the real API
//        client (mocked at the fetch seam, so the URL/method/body the client
//        actually sends is what is asserted).
//
// Fetch is stubbed globally in the style of tests/notifications.test.ts — a
// minimal Response covering exactly what src/api/client.ts reads — with a
// tiny in-memory scenario store standing in for the server.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../src/theme/ThemeProvider";
import { ToastProvider } from "../src/components/ui/Toast";
import { Forecast } from "../src/pages/Forecast";
import { ProjectionsPanel } from "../src/components/reports/ProjectionsPanel";
import { AppShell } from "../src/components/layout/AppShell";

/** Shared mutable server state — a plain top-level const would be re-created
 *  per module instance but the fetch stub is installed in `beforeEach`. */
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
  scenario_count: 0,
};

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

/** Bucket-END dates; the two dollar readings deliberately DIVERGE so F16 can
 *  tell them apart on screen. */
const BUCKETS = ["2026-11-06", "2027-11-06", "2028-10-06"];

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

  const copyMatch = /^\/api\/v1\/forecast\/scenarios\/([^/]+)\/copy$/.exec(path);
  if (copyMatch !== null) {
    const source = h.scenarios.find((entry) => entry.id === copyMatch[1]);
    if (source === undefined) {
      return fakeResponse(404, { detail: "Scenario not found", code: "scenario_not_found" });
    }
    const payload = (body ?? {}) as { id?: string; name?: string };
    const duplicate = {
      id: payload.id ?? `sc_copy_${h.scenarios.length}`,
      name: payload.name ?? `${source.name} (copy)`,
      events: source.events,
    };
    h.scenarios.push(duplicate);
    return fakeResponse(201, duplicate);
  }

  const oneMatch = /^\/api\/v1\/forecast\/scenarios\/([^/]+)$/.exec(path);
  if (oneMatch !== null) {
    const index = h.scenarios.findIndex((entry) => entry.id === oneMatch[1]);
    if (index < 0) {
      return fakeResponse(404, { detail: "Scenario not found", code: "scenario_not_found" });
    }
    if (method === "DELETE") {
      h.scenarios.splice(index, 1);
      return fakeResponse(204, null);
    }
    if (method === "PATCH") {
      h.scenarios[index] = body as { id: string; name: string; events: unknown[] };
      return fakeResponse(200, h.scenarios[index]);
    }
  }

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

const runCalls = () => h.calls.filter((call) => call.url.includes("/forecast/run"));

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
vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({
    user: {
      id: "m109-user",
      email: "m109@example.com",
      privacy_mode: false,
      currency_tracker_enabled: false,
      dashboard_preset: "overview",
    },
    logout: vi.fn(),
  }),
}));
// Toasts are incidental to every assertion here — mock the hook so a success
// message can never be mistaken for the row it announces.
vi.mock("../src/components/ui/Toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/components/ui/Toast")>();
  return {
    ...actual,
    useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  };
});
// Only the door to /forecast is under test here, so the panel's two calls are
// both stubbed: the defaults prefill, and (M157) the server-computed run.
vi.mock("../src/api/projections", () => ({
  // Resolved, not pending: React Query warns (stderr noise on an otherwise
  // green run) when a query function returns undefined.
  getProjectionDefaults: vi.fn().mockResolvedValue({ start_value_cad: null }),
  runProjections: vi.fn(),
}));
vi.mock("../src/lib/projectionEngine", () => ({
  ProjectionInputError: class ProjectionInputError extends Error {},
  runProjectionEngine: vi.fn(),
}));

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
// chart a fixed box so the axes and lines actually render (same shim as
// tests/projectionsRun.test.tsx).
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
          height: 340,
        });
      }
      return child;
    },
  };
});

function client(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderForecast(): void {
  render(
    <MemoryRouter initialEntries={["/forecast"]}>
      <QueryClientProvider client={client()}>
        <ThemeProvider>
          <Forecast />
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** Renders the panel inside a router so the entry point is the SPA <Link>. */
function renderPanel(): void {
  render(
    <MemoryRouter initialEntries={["/reports"]}>
      <QueryClientProvider client={client()}>
        <ThemeProvider>
          <ToastProvider>
            <ProjectionsPanel />
          </ToastProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** The real AppShell, exactly as tests/m100AccountsNav.test.tsx drives it. */
function renderShell(): void {
  render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <QueryClientProvider client={client()}>
        <ThemeProvider>
          <AppShell />
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  h.calls.length = 0;
  h.scenarios.length = 0;
  localStorage.clear();
  localStorage.setItem("theme", "light");
  localStorage.setItem("fin_tracker.tour.v1.m109-user", "completed");
  mocks.listAccounts.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  mocks.listCategories.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  vi.stubGlobal("fetch", vi.fn(handle));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// --- F15 --------------------------------------------------------------------

describe("F15 — the four boundary strings are on the page", () => {
  it("renders all four verbatim (docs/45 §Boundary)", () => {
    renderForecast();
    const required = [
      "Projection, not a prediction.",
      "Not a tax simulator — a scenario that changes income does not recalculate your tax projection.",
      "Deterministic, not Monte Carlo — the same inputs always give the same output.",
      "Not financial advice.",
    ];
    for (const line of required) {
      expect(
        screen.getAllByText(line).length,
        `missing boundary string: ${line}`,
      ).toBeGreaterThan(0);
    }
  });
});

// --- F16 --------------------------------------------------------------------

describe("F16 — the dollar toggle switches series with no refetch", () => {
  it("moves from future to today's dollars on the SAME run response", async () => {
    renderForecast();
    const chart = await screen.findByRole("img", { name: /Net worth forecast/ });

    const before = runCalls().length;
    expect(before).toBeGreaterThan(0);
    expect(chart.getAttribute("aria-label")).toContain(
      "value $100,000.00 to $120,000.00",
    );
    // The exact string the server sent is what the end-value cell prints.
    expect(screen.getByText("120000.00")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "Today's dollars" }));

    await waitFor(() => {
      expect(
        screen.getByRole("img", { name: /Net worth forecast/ }).getAttribute("aria-label"),
      ).toContain("value $99,000.00 to $115,000.00");
    });
    expect(screen.getByText("115000.00")).toBeTruthy();
    expect(screen.queryByText("120000.00")).toBeNull();
    // The whole point: one run, two readings.
    expect(runCalls().length).toBe(before);
  });
});

// --- F17 --------------------------------------------------------------------

describe("F17 — both surfaces point at /forecast", () => {
  it("the More panel exposes Forecast as the first Primary destination", async () => {
    // M185 (docs/47 §1.1): the rail is retired — Forecast now leads the More
    // panel's Primary group, directly after the five tabs that end in Reports
    // (docs/45: the what-if surface grows out of Reports).
    renderShell();
    fireEvent.click(await screen.findByTestId("more-toggle"));
    const panel = await screen.findByTestId("more-panel");
    const links = within(panel)
      .getAllByRole("link")
      .map((link) => link.getAttribute("href"));
    expect(links).toContain("/forecast");
    expect(links.indexOf("/forecast")).toBe(0);
    expect(
      within(panel).getByRole("link", { name: "Forecast" }).getAttribute("href"),
    ).toBe("/forecast");
  });

  it("the Reports projection card exposes “Open what-if scenarios”", async () => {
    renderPanel();
    const door = await screen.findByRole("link", { name: "Open what-if scenarios" });
    expect(door.getAttribute("href")).toBe("/forecast");
    // Added, not substituted: the calculator's own controls are still there.
    expect(screen.getByRole("button", { name: /Run projections/ })).toBeTruthy();
    expect(screen.getByLabelText("Start value (CAD)")).toBeTruthy();
  });
});

// --- F18 --------------------------------------------------------------------

describe("F18 — scenario create → copy → delete round-trips", () => {
  it("walks the whole cycle through the API client", async () => {
    renderForecast();
    await screen.findByRole("img", { name: /Net worth forecast/ });

    // CREATE
    fireEvent.click(screen.getByRole("button", { name: "New scenario" }));
    await screen.findByRole("checkbox", { name: "Overlay Scenario 1" });
    expect(h.scenarios).toHaveLength(1);
    const post = h.calls.filter(
      (call) => call.method === "POST" && call.url === "/api/v1/forecast/scenarios",
    );
    expect(post).toHaveLength(1);
    // The id is CLIENT-generated (docs/45 §2) and travels in the body.
    const created = post[0].body as { id: string; name: string; events: unknown[] };
    expect(created.id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    expect(created.name).toBe("Scenario 1");
    expect(created.events).toEqual([]);

    // COPY
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await screen.findByRole("checkbox", { name: "Overlay Scenario 1 (copy)" });
    expect(h.scenarios).toHaveLength(2);
    expect(
      h.calls.some(
        (call) =>
          call.method === "POST" &&
          call.url === `/api/v1/forecast/scenarios/${created.id}/copy`,
      ),
    ).toBe(true);

    // DELETE the copy, then the original — the list is empty again.
    fireEvent.click(screen.getByRole("button", { name: "Delete Scenario 1 (copy)" }));
    await waitFor(() => expect(h.scenarios).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "Delete Scenario 1" }));
    await waitFor(() => expect(h.scenarios).toHaveLength(0));

    const deletes = h.calls.filter((call) => call.method === "DELETE");
    expect(deletes.map((call) => call.url)).toEqual([
      "/api/v1/forecast/scenarios/sc_copy_1",
      `/api/v1/forecast/scenarios/${created.id}`,
    ]);

    // The empty state is back — the round trip closed.
    const title = await screen.findByTestId("empty-state-title");
    expect(title.textContent).toBe("No scenarios yet");
  });
});

// --- supporting contracts the spec calls out --------------------------------

describe("assumptions and horizon honesty", () => {
  it("the Assumptions panel spells out the flow split and every model caveat", async () => {
    renderForecast();
    await screen.findByRole("img", { name: /Net worth forecast/ });

    const toggle = screen.getByRole("button", { name: "Assumptions" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    // The double-count rule made visible: each total reads as its own parts.
    expect(screen.getByText(/5000\.00 = recurring 0\.00 \+ residual 5000\.00/)).toBeTruthy();
    expect(screen.getByText(/3800\.00 = recurring 450\.00 \+ residual 3350\.00/)).toBeTruthy();
    expect(screen.getByText(/Inflation: 2\.00%/)).toBeTruthy();

    // docs/45 §1.5 / §Boundary — every fixed disclosure, plus the server's own
    // disclaimer (which joins the header's copy, so it may appear twice).
    expect(screen.getAllByText(/House value is held constant at the purchase price/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/flat-rate monthly model/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/A career break suppresses ALL income/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Tax projection not recalculated by scenarios/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^Not Monte Carlo —/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Projection, not a prediction.").length).toBeGreaterThanOrEqual(2);
  });

  it("flags an event that lands past the rendered horizon instead of hiding it", async () => {
    h.scenarios.push({
      id: "sc_seed0001",
      name: "House in 2030",
      events: [
        {
          id: "ev_seed0001",
          type: "house_purchase",
          date: "2030-01-01",
          home_price_cad: "600000.00",
          down_payment_cad: "120000.00",
          mortgage_rate_pct: "5.00",
          amortization_years: 25,
        },
      ],
    });
    renderForecast();
    await screen.findByRole("img", { name: /Net worth forecast/ });

    // The run's last bucket is 2028-10-06; the event is a neutral heads-up
    // away, never silently dropped from the page.
    expect(screen.getByText(/After 2028-10-06/)).toBeTruthy();
    expect(screen.getByText(/House in 2030 — House purchase 2030-01-01/)).toBeTruthy();
  });
});

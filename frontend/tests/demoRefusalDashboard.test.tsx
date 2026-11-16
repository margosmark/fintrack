// demoRefusalDashboard — docs/47 §4 for the Dashboard layout preset picker
// (F-2 residual, card t_a51c9b96, item 2).
//
// The absolute pin: a `demo_account_restricted` response NEVER renders
// `role="alert"` or an `error-panel`. Before this wave `changePreset`'s catch
// called `toast.error("Could not save that layout", …)` — and Toast renders an
// error tone AS `role="alert"`, so a refused demo write was error-shaped.
//
// Pinned here:
//   * the segments' ACTION is the blocked write (`PATCH /users/me`), so the
//     group wears `demo-lock` and every segment binds (aria-describedby) to a
//     VISIBLE `DemoReason` — never `sr-only`;
//   * a refused save renders ONE `demo-refusal` (`role="status"`) + ONE
//     `toast.info`, zero `role="alert"`, zero `error-panel`, and the layout
//     still rolls back (M97);
//   * a LIVE tenant renders zero padlocks and zero reasons (§13 ex.1).
//
// The harness is m97DashboardPresets' — except that Toast stays REAL, because
// the whole point of this file is which tone reaches the DOM.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CARD_ORDER, Dashboard, type CardKey } from "../src/pages/Dashboard";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { ToastProvider } from "../src/components/ui/Toast";
import type { DashboardPreset } from "../src/types";

// --- jsdom shims -----------------------------------------------------------
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

// --- module mocks ----------------------------------------------------------
const auth = vi.hoisted(() => ({
  user: {
    id: "user-1",
    email: "demo@fintracker.local",
    privacy_mode: false,
    currency_tracker_enabled: true,
    llm_provider: "ollama",
    llm_base_url: null,
    llm_model: null,
    encryption_required: false,
    dashboard_preset: "overview",
  } as Record<string, unknown>,
  setUser: vi.fn(),
}));

// docs/47 §4: the guard reads the session through `useAuth`; the M136 optional
// scope is what shared leaves read. Stub BOTH exports, partially.
vi.mock("../src/auth/AuthContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/auth/AuthContext")>();
  return { ...actual, useAuth: () => ({ user: auth.user, setUser: auth.setUser }) };
});
vi.mock("../src/auth/authUser", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/auth/authUser")>();
  return { ...actual, useOptionalUser: () => auth.user };
});
vi.mock("../src/api/users", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/users")>()),
  updateMe: vi.fn(),
}));
vi.mock("../src/api/reports", () => ({
  getNetWorth: vi.fn(),
  getCashflow: vi.fn(),
  getSpendingCategories: vi.fn(),
}));
vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  listTransactions: vi.fn(),
  getRecurringCandidates: vi.fn(),
  suppressRecurringGroup: vi.fn(),
}));
vi.mock("../src/api/notifications", () => ({
  INBOX_QUERY_KEY: ["notifications", "inbox"],
  criticalBudgetItems: vi.fn(() => []),
  getNotificationInbox: vi.fn(() => Promise.resolve({ items: [] })),
  markNotificationRead: vi.fn(),
}));
vi.mock("../src/api/budgets", () => ({
  listBudgets: vi.fn(),
  createBudget: vi.fn(),
  updateBudget: vi.fn(),
  deleteBudget: vi.fn(),
}));
vi.mock("../src/api/insights", () => ({ getSpendingInsights: vi.fn() }));

import {
  getNetWorth,
  getCashflow,
  getSpendingCategories,
} from "../src/api/reports";
import { getRecurringCandidates, listTransactions } from "../src/api/transactions";
import { listBudgets } from "../src/api/budgets";
import { getSpendingInsights } from "../src/api/insights";
import { updateMe } from "../src/api/users";

/** One marker per card: text that exists in EVERY preset. */
const MARKERS: Array<[CardKey, string]> = [
  ["kpi", "Positions value"],
  ["insights", "Spending insights"],
  ["budgets", "No budgets set"],
  ["subscriptions", "No bill suggestions right now"],
  ["charts", "Spending by category (CAD)"],
  ["recent", "Recent transactions"],
];

/** The envelope the server really answers a refused demo write with. */
const DEMO_403 = {
  code: "demo_account_restricted",
  detail: "The demo account is read-only; changes are not allowed",
};

function mockData(): void {
  vi.mocked(updateMe).mockImplementation(async (patch) => {
    auth.user = { ...auth.user, ...patch };
    return auth.user as never;
  });
  vi.mocked(getNetWorth).mockResolvedValue({
    from: "2026-01-01",
    to: "2026-03-31",
    granularity: "month",
    buckets: [{ bucket_end: "2026-02-28", net_cad: "250.00" }],
  } as never);
  vi.mocked(getCashflow).mockResolvedValue({
    from: "2026-01-01",
    to: "2026-03-31",
    granularity: "month",
    buckets: [{ bucket_end: "2026-02-28", net_cad: "250.00" }],
  } as never);
  vi.mocked(getSpendingCategories).mockResolvedValue({
    items: [],
    total_cad: "0.00",
  } as never);
  vi.mocked(listTransactions).mockResolvedValue({
    items: [],
    total: 0,
    limit: 10,
    offset: 0,
  } as never);
  vi.mocked(listBudgets).mockResolvedValue([] as never);
  vi.mocked(getRecurringCandidates).mockResolvedValue(
    { groups: [], combined_monthly_total: "0.00", suppressed_count: 0, window_start: "2025-09-01" } as never,
  );
  vi.mocked(getSpendingInsights).mockResolvedValue({
    current_month_spending: "120.00",
    previous_month_spending: "300.00",
    month_over_month_pct: -60,
    daily_burn_rate: "6.00",
    projected_month_end: "180.00",
    days_elapsed: 20,
    days_in_month: 30,
    category_deltas: [],
  } as never);
}

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <ThemeProvider>
      <MemoryRouter>
        <QueryClientProvider client={client}>
          {/* REAL toasts: the pin is about which TONE reaches the DOM. */}
          <ToastProvider>
            <Dashboard />
          </ToastProvider>
        </QueryClientProvider>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

/** Document order of the six cards, as rendered. */
function cardOrder(): CardKey[] {
  const found = MARKERS.map(([key, text]) => ({ key, el: screen.getByText(text) }));
  found.sort((a, b) =>
    a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1,
  );
  return found.map((f) => f.key);
}

async function settled(): Promise<void> {
  for (const [, text] of MARKERS) {
    await screen.findByText(text);
  }
}

function pick(preset: DashboardPreset): void {
  const label = { overview: "Overview", import_first: "Import first", budget_first: "Budget first" }[
    preset
  ];
  fireEvent.click(screen.getByRole("tab", { name: label }));
}

function errorAlerts(): HTMLElement[] {
  return Array.from(document.querySelectorAll('[role="alert"]'));
}

beforeEach(() => {
  auth.user = {
    id: "user-1",
    email: "demo@fintracker.local",
    privacy_mode: false,
    currency_tracker_enabled: true,
    llm_provider: "ollama",
    llm_base_url: null,
    llm_model: null,
    encryption_required: false,
    dashboard_preset: "overview",
  };
  mockData();
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("docs/47 §4 — the Dashboard layout preset picker (F-2 item 2)", () => {
  it("demo: every segment binds (aria-describedby) to ONE VISIBLE reason and the group wears the padlock", async () => {
    renderDashboard();
    await settled();

    const tablist = screen.getByRole("tablist", { name: "Dashboard layout" });
    const segments = [...tablist.querySelectorAll('[role="tab"]')] as HTMLElement[];
    expect(segments).toHaveLength(3);
    for (const segment of segments) {
      expect(segment.getAttribute("aria-describedby")).toBe("dashboard-preset-demo-reason");
    }
    // The glyph rides with the control group (its parent span), and the reason
    // is a real, VISIBLE box below the row — never sr-only.
    expect(tablist.parentElement?.querySelector('[data-testid="demo-lock"]')).not.toBeNull();
    const reason = document.getElementById("dashboard-preset-demo-reason");
    expect(reason).not.toBeNull();
    expect(reason?.getAttribute("data-testid")).toBe("demo-reason");
    expect(reason?.className ?? "").not.toContain("sr-only");
    expect(reason?.textContent ?? "").toContain("read-only");
    // The segments stay ENABLED — an inert control cannot explain itself.
    for (const segment of segments) expect(segment.hasAttribute("disabled")).toBe(false);
  });

  it("demo: a refused save is ONE demo-refusal + ONE toast.info, never role=alert — and the layout rolls back", async () => {
    vi.mocked(updateMe).mockRejectedValue({ ...DEMO_403 });
    renderDashboard();
    await settled();

    pick("budget_first");
    // The rollback lands when the rejected PATCH is handled, not synchronously.
    await waitFor(() => expect(cardOrder()).toEqual(CARD_ORDER.overview));

    const refusals = await screen.findAllByTestId("demo-refusal");
    expect(refusals).toHaveLength(1);
    expect(refusals[0].getAttribute("role")).toBe("status");
    expect(screen.getByText("Save layout — the demo account is read-only")).toBeTruthy();
    expect(errorAlerts()).toHaveLength(0);
    expect(screen.queryByTestId("error-panel")).toBeNull();
    // The write really went out (the boundary stays server-authoritative).
    expect(updateMe).toHaveBeenCalledTimes(1);
    // No "Could not save that layout" error toast either.
    expect(screen.queryByText("Could not save that layout")).toBeNull();
  });

  it("live tenant: ZERO padlocks, ZERO reasons, ZERO refusals (§13 ex.1)", async () => {
    auth.user = { ...auth.user, email: "mark@example.com" };
    renderDashboard();
    await settled();

    expect(document.querySelectorAll('[data-testid="demo-lock"]')).toHaveLength(0);
    expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(0);
    expect(document.querySelectorAll('[data-testid="demo-refusal"]')).toHaveLength(0);
    const tablist = screen.getByRole("tablist", { name: "Dashboard layout" });
    for (const segment of [...tablist.querySelectorAll('[role="tab"]')]) {
      expect(segment.getAttribute("aria-describedby")).toBeNull();
    }
  });
});

// F-1 (docs/46 §D4) — the Dashboard's critical-budget banner is server-composed
// money PROSE rendered as one run of text, so the display mask has to run it
// through `maskAmountsInText` exactly the way the notifications-inbox body
// already does (AppShell.tsx). Before F-1 it rendered `{item.title} —
// {item.body}` raw: a masked session printed "Spent 312.84 of 150.00 CAD
// (209% of limit…)" beside a live region saying "Amounts masked. Dollar
// figures are hidden" — the KPIs beside it rendering •••••.
//
// This is the BEHAVIOURAL pin the finding asks for. `scripts/sweep_money.mjs`
// cannot see server-composed prose (it only proves component subscription), so
// these tests mount the REAL <Dashboard/> inside the REAL <AppShell/>, feed
// the real `criticalBudgetItems` selection the server's own notification
// shapes (docs/35) and read the DOM back:
//
//   1. masked session — no money-shaped text anywhere on /dashboard;
//   2. control — the same data unmasked prints the prose, so (1) cannot pass
//      because the banner never rendered;
//   3. a flip of the shipped chip re-masks the banner in place, no re-fetch.
//
// House matcher style: `.getAttribute(...)`, no jest-dom.
import type { ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../src/components/layout/AppShell";
import { ToastProvider } from "../src/components/ui/Toast";
import { Dashboard } from "../src/pages/Dashboard";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";
import { ThemeProvider } from "../src/theme/ThemeProvider";

const h = vi.hoisted(() => {
  const user = {
    id: "user-1",
    email: "mark@example.com",
    privacy_mode: false,
    currency_tracker_enabled: false,
    llm_provider: "openai",
    llm_base_url: null,
    llm_model: "gpt-4o-mini",
    encryption_required: false,
    dashboard_preset: "overview",
  };
  const brokerage = {
    id: 1,
    institution_id: null,
    name: "TFSA",
    type: "brokerage_non_registered",
    currency: "CAD",
    taxable: true,
    llm_autopilot: false,
    cash_balance: "0.00",
    investment_value: "0.00",
    total_value: "25000.00",
    starting_balance: "0.00",
    name_cipher: null,
  };
  const chequing = {
    ...brokerage,
    id: 2,
    name: "Everyday",
    type: "chequing" as const,
    taxable: false,
    total_value: "1840.55",
  };
  /** One positions row, CAD-listed so every figure is hand-checkable. */
  const position = (over: Record<string, unknown> = {}) => ({
    account_id: 1,
    account_name: "TFSA",
    security_id: 7,
    symbol: "AAA",
    listing_currency: "CAD",
    quantity: "100",
    price_date: "2026-10-09",
    price: "100.00",
    price_source: "manual",
    price_age_days: 0,
    price_stale: false,
    book_cost_cad: "9000.00",
    spark: ["90", "95", "100"],
    market_value_native: "10000.00",
    market_value_cad: "10000.00",
    fx_rate_to_cad: "1",
    previous_close_native: "90.00",
    previous_close_date: "2026-10-05",
    day_change_native: "10.00",
    day_change_pct: 11.11,
    day_change_cad: "1000.00",
    unrealized_gain_cad: "1000.00",
    unrealized_gain_native: "1000.00",
    unrealized_gain_pct: 11.11,
    ...over,
  });
  return {
    restoreSession: vi.fn(),
    me: vi.fn(),
    listAccounts: vi.fn(),
    getPositions: vi.fn(),
    getPortfolioValueHistory: vi.fn(),
    getWatchlist: vi.fn(),
    listSecurities: vi.fn(),
    listTrades: vi.fn(),
    getDividendSuggestions: vi.fn(),
    getExposure: vi.fn(),
    getBreakdown: vi.fn(),
    // Dashboard cards
    getNetWorth: vi.fn(),
    getCashflow: vi.fn(),
    getSpendingCategories: vi.fn(),
    listTransactions: vi.fn(),
    listBudgets: vi.fn(),
    getSpendingInsights: vi.fn(),
    listSubscriptions: vi.fn(),
    updateMe: vi.fn(),
    getNotificationInbox: vi.fn(),
    user,
    brokerage,
    chequing,
    position,
  };
});

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, restoreSession: h.restoreSession, me: h.me };
});
vi.mock("../src/api/accounts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/accounts")>();
  const { useQuery } = await import("@tanstack/react-query");
  return {
    ...actual,
    listAccounts: h.listAccounts,
    // The real hook calls `listAccounts` through the module's OWN binding,
    // which a vi.mock does not intercept — rebuild it on the mock.
    useInvestmentAccounts: () => {
      const accounts = useQuery({ queryKey: ["accounts", 200], queryFn: () => h.listAccounts(200) });
      const items = (accounts.data?.items ?? []) as Array<{
        type: keyof typeof actual.INVESTMENT_ACCOUNT_TYPES;
      }>;
      const investmentAccounts = items.filter(
        (a) => actual.INVESTMENT_ACCOUNT_TYPES[a.type] === true,
      );
      return { accounts, investmentAccounts };
    },
  };
});
vi.mock("../src/api/portfolio", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/portfolio")>();
  return {
    ...actual,
    getPositions: h.getPositions,
    getPortfolioValueHistory: h.getPortfolioValueHistory,
    getExposure: h.getExposure,
    getBreakdown: h.getBreakdown,
  };
});
vi.mock("../src/api/watchlist", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/watchlist")>();
  return { ...actual, getWatchlist: h.getWatchlist };
});
vi.mock("../src/api/securities", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/securities")>();
  return { ...actual, listSecurities: h.listSecurities };
});
vi.mock("../src/api/trades", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/trades")>();
  return {
    ...actual,
    listTrades: h.listTrades,
    getDividendSuggestions: h.getDividendSuggestions,
  };
});
vi.mock("../src/api/reports", () => ({
  getNetWorth: h.getNetWorth,
  getCashflow: h.getCashflow,
  getSpendingCategories: h.getSpendingCategories,
}));
vi.mock("../src/api/transactions", () => ({
  listTransactions: h.listTransactions,
  mergeTransactionSplit: vi.fn(),
}));
// The SELECTION stays real: `criticalBudgetItems` is the same filter the app
// runs over the inbox page, so the pin exercises the shipping shape end to
// end (server prose -> selection -> banner -> masker), not a stand-in.
vi.mock("../src/api/notifications", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/notifications")>();
  return {
    ...actual,
    getNotificationInbox: h.getNotificationInbox,
    markNotificationRead: vi.fn(),
    markAllNotificationsRead: vi.fn(),
  };
});
vi.mock("../src/api/budgets", () => ({
  listBudgets: h.listBudgets,
  createBudget: vi.fn(),
  updateBudget: vi.fn(),
  deleteBudget: vi.fn(),
}));
vi.mock("../src/api/insights", () => ({ getSpendingInsights: h.getSpendingInsights }));
vi.mock("../src/api/subscriptions", () => ({ listSubscriptions: h.listSubscriptions }));
vi.mock("../src/api/users", () => ({ updateMe: h.updateMe }));
// The shell and three pages read `useAuth()`. Mocking the module (rather than
// mounting the real AuthProvider) keeps the harness honest about what M140 is
// testing: the real provider would call `initMaskForSession()` when the
// identity resolves and silently re-mask mid-assertion. Same shape as
// tests/m139MaskChip.test.tsx.
vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({
    user: h.user,
    setUser: vi.fn(),
    logout: vi.fn(),
  }),
  isReadOnlySession: () => false,
}));

// jsdom has no matchMedia; ThemeProvider (prefers-color-scheme), the shell's
// 768px breakpoint and framer-motion's reduced-motion check all ask for it.
// QUERY-AWARE like tests/m139MaskChip.test.tsx: any min-width query is true so
// the shell keeps its desktop rail (the chip is icon-only below 768).
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

/** Stand-in EventSource so the SSE path can be driven by hand. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  close(): void {}
}

function positionsPayload(over: Record<string, unknown> = {}) {
  return {
    items: [h.position()],
    total: 1,
    total_unrealized_cad: "1000.00",
    total_value_cad: "10000.00",
    cash_cad: "0.00",
    portfolio_value_cad: "10000.00",
    total_day_change_cad: "1000.00",
    total_day_change_pct: 11.11,
    day_change_as_of: "2026-10-05",
    day_change_mixed_baseline: false,
    ...over,
  };
}

const BUCKETS = [
  {
    bucket_end: "2026-01-31",
    cash_cad: "1000.00",
    positions_cad: "400.00",
    net_cad: "1400.00",
    assets_cad: "1500.00",
    liabilities_cad: "100.00",
  },
  {
    bucket_end: "2026-02-28",
    cash_cad: "1200.00",
    positions_cad: "400.00",
    net_cad: "1600.00",
    assets_cad: "1700.00",
    liabilities_cad: "100.00",
  },
];

/** The server's own notification shapes (docs/35 §Inbox union), verbatim —
 *  money sits INSIDE the prose, which is exactly why the banner has to run it
 *  through `maskAmountsInText` (docs/46 §D4). The `209%` / `100%` tail is the
 *  percentage carve-out: proportional values are not sensitive, so they
 *  survive the mask and the money scan parks them (operator ruling). */
const BUDGET_ALERT_1 = {
  id: "budget_threshold:9001",
  kind: "budget_threshold" as const,
  severity: "critical" as const,
  title: "Budget alert: Groceries",
  body: "Spent 312.84 of 150.00 CAD (209% of limit; threshold 100%)",
  link: "/budgets",
  amount_cad: "312.84",
  occurred_at: "2026-10-15T12:00:00Z",
  read: false,
  read_at: null,
};

/** A second critical item with a THOUSANDS separator — the comma shape the
 *  acceptance regex (`\d{1,3}(,\d{3})*\.\d{2}`) is written around. */
const BUDGET_ALERT_2 = {
  id: "budget_threshold:9002",
  kind: "budget_threshold" as const,
  severity: "critical" as const,
  title: "Budget alert: Dining out",
  body: "Spent 1,240.00 of 600.00 CAD (207% of limit; threshold 100%)",
  link: "/budgets",
  amount_cad: "1240.00",
  occurred_at: "2026-10-15T11:00:00Z",
  read: false,
  read_at: null,
};

const BUDGET_ALERTS = [BUDGET_ALERT_1, BUDGET_ALERT_2];

/** The matching `/budgets` row, so the page carries the same figures on two
 *  surfaces — the whole-dollar card ("$313 / $150") and the prose banner. */
const BUDGET = {
  id: 1,
  user_id: 1,
  category_id: 5,
  amount: "150.00",
  period: "monthly",
  carry: "off",
  category_name: "Groceries",
  spent: "312.84",
  carried_in: null,
  remaining: "-162.84",
  pct_used: 208.56,
};

function mockAllData(): void {
  h.restoreSession.mockResolvedValue({ access_token: "t", token_type: "bearer", user: h.user });
  h.me.mockResolvedValue(h.user);
  h.listAccounts.mockResolvedValue({
    items: [h.brokerage, h.chequing],
    total: 2,
    limit: 200,
    offset: 0,
  });
  h.getPositions.mockResolvedValue(positionsPayload());
  h.getPortfolioValueHistory.mockResolvedValue({
    account_ids: [1],
    from: "2026-01-01",
    to: "2026-03-31",
    granularity: "month",
    requested_granularity: "month",
    currency: "CAD",
    native_currency: "CAD",
    min_event_value: "1000.00",
    has_data: true,
    buckets: BUCKETS.map((b) => ({ ...b, value_cad: b.net_cad, estimated: false, valued: true })),
    events: [],
    stats: {
      from_bucket: "2026-01-31",
      to_bucket: "2026-02-28",
      from_value_cad: "1400.00",
      to_value_cad: "1600.00",
      change_cad: "200.00",
      change_pct: 14.29,
    },
  });
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  h.getExposure.mockResolvedValue({ items: [], total: 0 });
  h.getBreakdown.mockResolvedValue({
    dimension: "sector",
    items: [],
    total_cad: "0",
    unclassified_cad: "0",
  });
  h.getNetWorth.mockResolvedValue({
    from: "2026-01-01",
    to: "2026-03-31",
    granularity: "month",
    buckets: BUCKETS,
  });
  h.getCashflow.mockResolvedValue({
    from: "2026-01-01",
    to: "2026-03-31",
    granularity: "month",
    buckets: [{ bucket_end: "2026-02-28", net_cad: "250.00" }],
  });
  h.getSpendingCategories.mockResolvedValue({ items: [], total_cad: "0.00" });
  h.listTransactions.mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 });
  h.listBudgets.mockResolvedValue([BUDGET]);
  h.getSpendingInsights.mockResolvedValue({
    current_month_spending: "120.00",
    previous_month_spending: "300.00",
    month_over_month_pct: -60,
    daily_burn_rate: "6.00",
    projected_month_end: "180.00",
    days_elapsed: 20,
    days_in_month: 30,
    // A FRACTIONAL percentage on purpose: "12.34%" is dot-and-cents shaped
    // and must survive a mask (docs/46 §D2), which is why the scan below
    // parks percentages exactly the way `maskAmountsInText` does.
    category_deltas: [
      { category_id: 5, category_name: "Groceries", change_pct: 12.34 },
    ],
  });
  h.listSubscriptions.mockResolvedValue({ subscriptions: [] });
  h.getNotificationInbox.mockResolvedValue({
    items: [BUDGET_ALERT_1, BUDGET_ALERT_2],
    total: BUDGET_ALERTS.length,
    unread_count: BUDGET_ALERTS.length,
    limit: 50,
    cursor: null,
    next_cursor: null,
  });
}

/** The real shell over the real routes — the shape the bug lived in. */
function renderRouted(entry: string, ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = (
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[entry]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route path="*" element={ui} />
              </Route>
            </Routes>
          </MemoryRouter>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
  return { ...render(tree), client, tree };
}

/** Every API mock's call count, as one comparable fingerprint. */
function fetchFingerprint(): number {
  return [
    h.listAccounts,
    h.getPositions,
    h.getPortfolioValueHistory,
    h.getWatchlist,
    h.listSecurities,
    h.listTrades,
    h.getDividendSuggestions,
    h.getExposure,
    h.getBreakdown,
    h.getNetWorth,
    h.getCashflow,
    h.getSpendingCategories,
    h.listTransactions,
    h.listBudgets,
    h.getSpendingInsights,
    h.listSubscriptions,
    h.getNotificationInbox,
    h.restoreSession,
    h.me,
  ].reduce((sum, fn) => sum + fn.mock.calls.length, 0);
}


/**
 * Money-shaped text still on screen — `312.84`, `1,240.00`, `$1,600.00`: the
 * shapes a shoulder-surfer reads as dollars. This is the acceptance scan for
 * F-1, and it deliberately matches the finding's regex
 * `\d{1,3}(,\d{3})*\.\d{2}`.
 *
 * ISO dates park first (they carry dots in neither shape, but be explicit),
 * then percentages — parked EXACTLY the way `maskAmountsInText` parks them,
 * because docs/46 §D2 rules proportional values non-sensitive: "209% of
 * limit" and a "12.34%" category delta stay readable on a masked screen, and
 * neither is a dollar figure.
 */
function moneyLeaks(root: HTMLElement): string[] {
  const parked = (root.textContent ?? "")
    .replace(/\d{4}-\d{2}-\d{2}/g, " ")
    .replace(/\d+(?:\.\d+)?\s?%/g, " ");
  return parked.match(/\d{1,3}(?:,\d{3})*\.\d{2}/g) ?? [];
}

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
  FakeEventSource.instances = [];
  mockAllData();
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  vi.spyOn(globalThis, "fetch");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  sessionStorage.clear();
  initMaskForSession();
  vi.clearAllMocks();
});

describe("F-1 — the Dashboard critical-budget banner runs through the display mask", () => {
  it("masked session: /dashboard holds NO money-shaped text anywhere — banner included", async () => {
    setMasked(true);
    const { container } = renderRouted("/dashboard", <Dashboard />);

    // The banner renders at all (2 unread critical items -> the plural header),
    // so a later `[]` scan can never be a pass on an empty page.
    await screen.findByText("2 budget thresholds breached");

    // The prose around the figures masks; the sentence, the category name and
    // the percentage survive (docs/46 §D4 + §D2).
    const line = await screen.findByText(/Budget alert: Groceries/);
    await waitFor(() => expect(line.textContent).toContain(MASK_TEXT));
    expect(line.textContent).not.toContain("312.84");
    expect(line.textContent).not.toContain("150.00");
    expect(line.textContent).toContain("209%");

    // The acceptance criterion, whole-viewport rather than one text node: the
    // frame that ANNOUNCES "Amounts masked" (below) carries no readable
    // dollar figure on any surface — banner, KPI strip, budget cards,
    // insights, chart.
    expect(moneyLeaks(container)).toEqual([]);
    expect(container.textContent).toContain("Amounts masked. Dollar figures are hidden");
  });

  it("control: unmasked, the same inbox prints the server prose verbatim", async () => {
    const { container } = renderRouted("/dashboard", <Dashboard />);
    await screen.findByText("2 budget thresholds breached");

    const line = await screen.findByText(/Budget alert: Groceries/);
    expect(line.textContent).toContain(
      "Budget alert: Groceries — Spent 312.84 of 150.00 CAD (209% of limit; threshold 100%)",
    );
    // The scanner's inverse — it FINDS the figures when the mask is off, so
    // the `[]` above is a real result and not an empty-page artifact.
    expect(moneyLeaks(container)).toContain("312.84");
    expect(moneyLeaks(container)).toContain("150.00");
    expect(moneyLeaks(container)).toContain("1,240.00");
    // …and the percentage carve-out is symmetric: both sides keep "12.34%".
    expect(container.textContent).toContain("12.34%");
  });

  it("a flip of the shipped chip re-masks the banner IN PLACE, with no re-fetch", async () => {
    renderRouted("/dashboard", <Dashboard />);
    await screen.findByText("2 budget thresholds breached");
    const line = await screen.findByText(/Budget alert: Groceries/);
    expect(line.textContent).toContain("Spent 312.84 of 150.00 CAD");
    // The percentage carve-out holds while unmasked too (it is readable both
    // ways — that is what "not sensitive" means).
    expect((document.body.textContent ?? "").includes("12.34%")).toBe(true);

    const before = fetchFingerprint();
    fireEvent.click(screen.getByTestId("mask-chip"));
    await waitFor(() =>
      expect(screen.getByTestId("mask-chip").getAttribute("aria-pressed")).toBe("true"),
    );

    // SAME DOM node (a flip re-renders, never remounts — docs/46 §D6), now
    // bullets. The scan is scoped to the banner: the full-viewport flip scan
    // is the chart-axis contract F-7 owns (t_fe3547f4 / t_c3e65cd5), not this
    // finding's.
    await waitFor(() => expect(line.textContent).toContain(MASK_TEXT));
    expect(screen.queryAllByText(/Budget alert: Groceries/)[0]).toBe(line);
    expect(moneyLeaks(line as HTMLElement)).toEqual([]);
    // …and the banner's data was not re-fetched to get there.
    expect(fetchFingerprint()).toBe(before);

    // Back the other way: the prose returns from the SAME node.
    fireEvent.click(screen.getByTestId("mask-chip"));
    await waitFor(() => expect(line.textContent).toContain("Spent 312.84 of 150.00 CAD"));
    expect(fetchFingerprint()).toBe(before);
  });
});

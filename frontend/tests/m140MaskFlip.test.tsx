// M140 — the mask flip is INSTANT on every money surface, with no refresh and
// no re-fetch (docs/46 §D6).
//
// M139 shipped the store and the formatters; M140 is the bug Mark hit live —
// "hitting the mask/unmask does not happen in real time, needs a page refresh."
// The formatters read `isMasked()` at CALL time, so any component that renders
// money has to RE-RENDER on a flip. M139 relied on one `useMasked()` in
// AppShell walking down through `<Outlet/>`, which works only while every
// money string is built inline in the owning component. Top-of-page money that
// came out of custom string-building (the portfolio header's `formatCad`, the
// Dashboard KPI strip's `fmtCadCompact`/`fmtDelta`, the accounts list's
// `formatMoney`) never re-rendered, so the old figure sat on the glass until
// the next query/cache event — which is why a reload "fixed" it.
//
// This file pins the FIXED contract, three ways:
//
//   1. the real AppShell + real routes (/investments, /accounts): one flip of
//      the shipped chip re-masks every money callout IN PLACE — same DOM nodes,
//      no remount — and fires ZERO network calls;
//   2. the Dashboard KPI strip: the delta chip (`fmtDelta`, a currency builder
//      that never consulted the mask before M140) and the HelpHint derivation
//      (`fmtCad`) flip with the rest;
//   3. the SSE live-tick path (M126/M130): a tick arriving on a MASKED session
//      must re-render the recomputed figures as bullets — a masked UI that
//      flashes a real number on the next tick is a privacy failure, not a
//      cosmetic one.
//
// House matcher style: `.getAttribute(...)`, no jest-dom.
import type { ReactElement } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../src/components/layout/AppShell";
import { ToastProvider } from "../src/components/ui/Toast";
import { Accounts } from "../src/pages/Accounts";
import { Dashboard } from "../src/pages/Dashboard";
import { Investments } from "../src/pages/Investments";
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
vi.mock("../src/api/notifications", () => ({
  INBOX_QUERY_KEY: ["notifications", "inbox"],
  criticalBudgetItems: vi.fn(() => []),
  getNotificationInbox: h.getNotificationInbox,
  markNotificationRead: vi.fn(),
  markAllNotificationsRead: vi.fn(),
  unreadBadgeLabel: () => "",
}));
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
  h.listBudgets.mockResolvedValue([]);
  h.getSpendingInsights.mockResolvedValue({
    current_month_spending: "120.00",
    previous_month_spending: "300.00",
    month_over_month_pct: -60,
    daily_burn_rate: "6.00",
    projected_month_end: "180.00",
    days_elapsed: 20,
    days_in_month: 30,
    category_deltas: [],
  });
  h.listSubscriptions.mockResolvedValue({ subscriptions: [] });
  h.getNotificationInbox.mockResolvedValue({ items: [], unread_count: 0 });
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

/** All `$…` money callouts currently on screen (the mask's job is to eat them). */
function moneyCallouts(root: HTMLElement): string[] {
  return [...root.querySelectorAll("*")]
    .flatMap((el) => [...el.childNodes])
    .filter((n): n is Text => n.nodeType === Node.TEXT_NODE)
    .map((n) => n.textContent ?? "")
    .flatMap((t) => t.match(/\$-?[\d,.]+[KMB]?/g) ?? []);
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

describe("M140 — one chip flip re-masks the routed tree in place", () => {
  it("/investments: the portfolio header, day change, range stats and Σ row all flip, with no remount and no fetch", async () => {
    const { container } = renderRouted("/investments", <Investments />);

    const value = await screen.findByTestId("portfolio-value");
    expect(value.textContent).toContain("$10,000.00");
    const day = await screen.findByTestId("portfolio-day-change");
    expect(day.textContent).toContain("+$1,000.00");
    const total = await screen.findByTestId("positions-total-row");
    expect(total.textContent).toContain("$10,000.00");
    expect(moneyCallouts(container).length).toBeGreaterThan(3);

    // Pin the DOM nodes: a flip must RE-RENDER, never remount (a remount
    // would drop component state and, with react-query, re-fetch).
    const nodesBefore = {
      value,
      day,
      total,
      accounts: h.listAccounts.mock.calls.length,
    };
    const before = fetchFingerprint();

    fireEvent.click(screen.getByTestId("mask-chip"));
    await waitFor(() => expect(screen.getByTestId("mask-chip").getAttribute("aria-pressed")).toBe("true"));

    // SAME nodes, now bullets.
    expect(screen.getByTestId("portfolio-value")).toBe(nodesBefore.value);
    expect(screen.getByTestId("portfolio-day-change")).toBe(nodesBefore.day);
    expect(screen.getByTestId("positions-total-row")).toBe(nodesBefore.total);
    expect(screen.getByTestId("portfolio-value").textContent).toContain(MASK_TEXT);
    expect(screen.getByTestId("portfolio-day-change").textContent).toContain(MASK_TEXT);
    expect(screen.getByTestId("positions-total-row").textContent).toContain(MASK_TEXT);
    // The whole surface — not just the three testids the M139 happy path walked.
    expect(moneyCallouts(container)).toEqual([]);

    // Zero network: no refetch, no invalidate, no session re-probe.
    expect(fetchFingerprint()).toBe(before);

    // …and back, still with no fetch.
    fireEvent.click(screen.getByTestId("mask-chip"));
    await waitFor(() => expect(screen.getByTestId("mask-chip").getAttribute("aria-pressed")).toBe("false"));
    expect(screen.getByTestId("portfolio-value").textContent).toContain("$10,000.00");
    expect(moneyCallouts(container).length).toBeGreaterThan(3);
    expect(fetchFingerprint()).toBe(before);
  });

  it("/accounts: the list balances flip instantly — page AND More panel", async () => {
    const { container } = renderRouted("/accounts", <Accounts />);

    // Two surfaces print these balances: the page's grouped list, and the
    // shell's More panel (the same `formatMoney` per account; M185 moved it
    // out of the retired rail). A flip has to re-mask BOTH — the panel is
    // what a user sees while the page is scrolled away. So four callouts,
    // two distinct figures.
    fireEvent.click(await screen.findByTestId("more-toggle"));
    await screen.findByTestId("more-panel");
    await waitFor(() => expect(moneyCallouts(container).length).toBe(4));
    expect([...new Set(moneyCallouts(container))].sort()).toEqual(
      ["$1,840.55", "$25,000.00"].sort(),
    );

    const before = fetchFingerprint();
    setMasked(true);
    await waitFor(() => expect(moneyCallouts(container)).toEqual([]));
    expect(container.textContent).not.toContain("$25,000.00");
    expect(container.textContent).not.toContain("$1,840.55");
    expect(fetchFingerprint()).toBe(before);

    setMasked(false);
    await waitFor(() => expect(moneyCallouts(container).length).toBe(4));
    expect(fetchFingerprint()).toBe(before);
  });

  it("a keyboard ⌘⇧H flip reaches the header too — the shortcut and the chip agree", async () => {
    renderRouted("/investments", <Investments />);
    const value = await screen.findByTestId("portfolio-value");
    expect(value.textContent).toContain("$10,000.00");

    fireEvent.keyDown(window, { key: "H", metaKey: true, shiftKey: true });
    await waitFor(() => expect(screen.getByTestId("mask-chip").getAttribute("aria-pressed")).toBe("true"));
    expect(screen.findByTestId("portfolio-value")).toBeTruthy();
    await waitFor(() =>
      expect(screen.getByTestId("portfolio-value").textContent).toContain(MASK_TEXT),
    );
  });
});

describe("M140 — the Dashboard KPI strip", () => {
  it("the delta chip (fmtDelta) and the HelpHint derivation flip with the strip", async () => {
    const { container } = renderRouted("/dashboard", <Dashboard />);

    // The KPI strip renders AnimatedNumber (already M139-subscribed) plus two
    // CUSTOM string paths that M140 had to fix: `fmtDelta` (the currency
    // builder behind each delta chip — `$200` here) and the net-worth
    // HelpHint (`fmtCad` assets − liabilities).
    await waitFor(() => expect(screen.getByText("Net worth")).toBeTruthy());
    await waitFor(() => expect(moneyCallouts(container).length).toBeGreaterThan(0));
    const beforeText = container.textContent ?? "";
    expect(beforeText).toContain("$200"); // the net_cad delta chip

    const before = fetchFingerprint();
    setMasked(true);

    // AnimatedNumber's bullets, the delta chips' bullets, and the HelpHint's
    // bullets — a masked dashboard may not print a single dollar figure.
    await waitFor(() => expect(moneyCallouts(container)).toEqual([]));
    expect(screen.getAllByText(MASK_TEXT).length).toBeGreaterThan(1);
    expect(fetchFingerprint()).toBe(before);

    setMasked(false);
    await waitFor(() => expect(container.textContent).toContain("$200"));
    expect(fetchFingerprint()).toBe(before);
  });
});

describe("M140 — the SSE live-tick path re-masks", () => {
  it("a tick on a MASKED session re-renders the recomputed headline as bullets, never a real number", async () => {
    setMasked(true);
    renderRouted("/investments", <Investments />);

    const value = await screen.findByTestId("portfolio-value");
    expect(value.textContent).toContain(MASK_TEXT);

    // Wait for the stream, open it, then push a quote that MOVES the row —
    // the header re-evaluates its headline from server primitives and labels
    // it "live" (M130). Under a mask that recomputation must still be bullets.
    await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0));
    act(() => {
      FakeEventSource.instances[0].onopen?.(new Event("open"));
    });
    act(() => {
      FakeEventSource.instances[0].onmessage?.({
        data: JSON.stringify([
          {
            security_id: 7,
            symbol: "AAA",
            price: "150.00",
            date: "2026-10-10",
            source: "manual",
            age_days: 0,
            stale: false,
          },
        ]),
      } as MessageEvent);
    });

    // The tick landed: the header says "live"…
    await waitFor(() => expect(screen.getByText("live")).toBeTruthy());
    // …and every figure it recomputed is still masked.
    expect(screen.getByTestId("portfolio-value").textContent).toContain(MASK_TEXT);
    expect(screen.getByTestId("portfolio-day-change").textContent).toContain(MASK_TEXT);
    expect(moneyCallouts(screen.getByTestId("portfolio-value"))).toEqual([]);

    // Unmasking shows the LIVE recomputation (150 × 100 = $15,000), not the
    // pre-tick server figure — the tick and the flip are independent axes.
    setMasked(false);
    await waitFor(() =>
      expect(screen.getByTestId("portfolio-value").textContent).toContain("$15,000.00"),
    );
  });
});

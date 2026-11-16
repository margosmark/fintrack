// M65 / m61 #7 — behaviour: the header stream indicator must not stay a
// neutral green "Stream connected" while any rendered price row is older than
// 24h (the demo's "manual · 4d" rows). Connection state and data freshness were
// BOTH neutral before; the fix gives the indicator a "prices stale" state that
// overrides the connected label (a red "Connection error" still wins).
//
// Harness mirrors jargonLeaks.test.tsx (real pages, mocked domain APIs) and
// usePriceStream.test.ts.ts (FakeEventSource) — the Investments page is
// rendered exactly as the shell serves it.
import type { ReactElement } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";

const h = vi.hoisted(() => {
  const user = {
    id: "user-1",
    email: "owner@example.com",
    privacy_mode: false,
    currency_tracker_enabled: false,
    llm_provider: "openai",
    llm_base_url: null,
    llm_model: "gpt-4o-mini",
    encryption_required: false,
  };
  const brokerage = {
    id: 1,
    institution_id: null,
    name: "Margin account",
    type: "brokerage_non_registered",
    currency: "CAD",
    taxable: true,
    llm_autopilot: false,
    cash_balance: "0.00",
    investment_value: "0.00",
    total_value: "0.00",
    starting_balance: "0.00",
    name_cipher: null,
  };
  const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 200, offset: 0 });
  return {
    restoreSession: vi.fn(),
    me: vi.fn(),
    listAccounts: vi.fn(),
    listCategories: vi.fn(),
    getPositions: vi.fn(),
    getPortfolioValueHistory: vi.fn(),
    getWatchlist: vi.fn(),
    listSecurities: vi.fn(),
    listTrades: vi.fn(),
    getDividendSuggestions: vi.fn(),
    user,
    brokerage,
    page,
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
    // The real hook calls `listAccounts` through accounts.ts's OWN module
    // binding, which a vi.mock does not intercept — rebuild it on the mock.
    useInvestmentAccounts: () => {
      const accounts = useQuery({
        queryKey: ["accounts", 200],
        queryFn: () => h.listAccounts(200),
      });
      const investmentAccounts = (accounts.data?.items ?? []).filter(
        (a) => actual.INVESTMENT_ACCOUNT_TYPES[a.type] === true,
      );
      return { accounts, investmentAccounts };
    },
  };
});
vi.mock("../src/api/categories", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/categories")>();
  return { ...actual, listCategories: h.listCategories };
});
vi.mock("../src/api/portfolio", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/portfolio")>();
  return { ...actual, getPositions: h.getPositions, getPortfolioValueHistory: h.getPortfolioValueHistory };
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

/** jsdom has no matchMedia (ThemeProvider reads prefers-color-scheme). */
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

/** Minimal EventSource stand-in (usePriceStream.test.ts): the hook assigns
 *  onopen/onmessage/onerror properties, so firing onopen flips the indicator
 *  to `connected` — exactly the m61 "connected while stale" contradiction. */
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

function renderPage(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <ToastProvider>
            <AuthProvider>
              <Routes>
                <Route path="*" element={ui} />
              </Routes>
            </AuthProvider>
          </ToastProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** A position row whose price is `ageDays` days old (0 = priced today). */
function positions(ageDays: number) {
  return {
    items: [
      {
        account_id: 1,
        account_name: "Margin account",
        security_id: 7,
        symbol: "RY.TO",
        listing_currency: "CAD",
        quantity: "10",
        price_date: ageDays === 0 ? "2026-09-29" : "2026-09-25",
        price: "100.00",
        price_source: "manual",
        price_age_days: ageDays,
        price_stale: ageDays > 7,
        book_cost_cad: "1000.00",
        spark: [],
        market_value_native: "1000.00",
        market_value_cad: "1000.00",
      },
    ],
    total: 1,
  };
}

async function openStream(): Promise<void> {
  await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0));
  act(() => {
    FakeEventSource.instances[0].onopen?.(new Event("open"));
  });
}

beforeEach(() => {
  FakeEventSource.instances = [];
  h.restoreSession.mockResolvedValue({
    access_token: "token",
    token_type: "bearer",
    user: h.user,
  });
  h.me.mockResolvedValue(h.user);
  h.listAccounts.mockResolvedValue(h.page([h.brokerage]));
  h.listCategories.mockResolvedValue(h.page([]));
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("stream indicator reports price staleness (M65 / m61 #7)", () => {
  it("shows 'prices stale', not 'Stream connected', while rows are older than 24h", async () => {
    h.getPositions.mockResolvedValue(positions(4));

    renderPage(<Investments />);
    await openStream();

    // The stream IS connected — yet a 4-day-old row must not sit under a
    // neutral green "Stream connected".
    expect(await screen.findByText("prices stale")).toBeTruthy();
    expect(screen.queryByText("Stream connected")).toBeNull();
  });

  it("keeps 'Stream connected' when every row is priced today", async () => {
    h.getPositions.mockResolvedValue(positions(0));

    renderPage(<Investments />);
    await openStream();

    expect(await screen.findByText("Stream connected")).toBeTruthy();
    expect(screen.queryByText("prices stale")).toBeNull();
  });
});

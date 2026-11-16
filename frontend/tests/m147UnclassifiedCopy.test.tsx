// M147 — the Unclassified honesty copy (docs/09):
//
//   the exposure breakdown names every fund that landed wholly in
//   Unclassified because its composition rows were never imported, with
//   its market value and the one action that fills the slice; a payload
//   without the field (older API) renders no note at all.
//
// Harness mirrors m126PriceHistory.test.tsx: the real Investments page,
// the real query client, mocked domain APIs.
import type { ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    name: "TFSA",
    type: "brokerage_rrsp",
    currency: "CAD",
    taxable: false,
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
    getExposure: vi.fn(),
    getBreakdown: vi.fn(),
    getWatchlist: vi.fn(),
    listSecurities: vi.fn(),
    listTrades: vi.fn(),
    getDividendSuggestions: vi.fn(),
    getPriceHistory: vi.fn(),
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
  return { ...actual, listTrades: h.listTrades, getDividendSuggestions: h.getDividendSuggestions };
});
vi.mock("../src/api/prices", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/prices")>();
  return { ...actual, getPriceHistory: h.getPriceHistory };
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

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
}

function renderPage(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/investments"]}>
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

const BREAKDOWN_ROW = {
  key: "United States",
  market_value_cad: "900.00",
  weight_pct: 60,
};

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
  h.getExposure.mockResolvedValue({ items: [], total: 0 });
  h.getBreakdown.mockResolvedValue({
    dimension: "geography",
    items: [BREAKDOWN_ROW],
    total_cad: "1500.00",
    unclassified_cad: "600.00",
    unclassified_funds: [
      {
        security_id: 41,
        symbol: "VGRO",
        name: "Vanguard Growth ETF Portfolio",
        market_value_cad: "600.00",
      },
    ],
  });
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  h.getPositions.mockResolvedValue({ items: [], total: 0 });
  h.getPriceHistory.mockResolvedValue({ items: [], security: null });
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function openGeographyTab() {
  renderPage(<Investments />);
  fireEvent.click(await screen.findByRole("tab", { name: "Geography" }));
  await screen.findByLabelText("Exposure breakdown table");
}

describe("M147 — the Unclassified honesty copy", () => {
  it("names each not-yet-imported fund with its value and the Import action", async () => {
    await openGeographyTab();

    await waitFor(() => {
      expect(
        screen.getByText(/Unclassified because these funds' holdings haven't been imported yet/),
      ).toBeTruthy();
    });
    expect(screen.getByText(/VGRO — Vanguard Growth ETF Portfolio/)).toBeTruthy();
    expect(screen.getByText(/\$600\.00/)).toBeTruthy();
    expect(screen.getByText(/Import holdings to fill this slice/)).toBeTruthy();
  });

  it("explains a top-slice-only fund differently (reason=partial)", async () => {
    h.getBreakdown.mockResolvedValue({
      dimension: "geography",
      items: [BREAKDOWN_ROW],
      total_cad: "1500.00",
      unclassified_cad: "600.00",
      unclassified_funds: [
        {
          security_id: 77,
          symbol: "VTI",
          name: "Vanguard Total Stock Market ETF",
          market_value_cad: "600.00",
          reason: "partial",
          imported_weight_pct: "33.50",
        },
      ],
    });

    await openGeographyTab();

    await screen.findByText(/VTI — Vanguard Total Stock Market ETF/);
    expect(screen.getByText(/only 33\.5% of this fund is imported/)).toBeTruthy();
    expect(screen.getByText(/waits on a fuller source/)).toBeTruthy();
    expect(screen.queryByText(/use Import holdings/)).toBeNull();
  });

  it("renders no note when the payload has no unclassified_funds (older API)", async () => {
    h.getBreakdown.mockResolvedValue({
      dimension: "geography",
      items: [BREAKDOWN_ROW],
      total_cad: "1500.00",
      unclassified_cad: "600.00",
    });

    await openGeographyTab();

    await screen.findByLabelText("Exposure breakdown table");
    expect(screen.queryByText(/haven't been imported yet/)).toBeNull();
  });
});

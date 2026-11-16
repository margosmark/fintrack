// M186 (docs/09 §Watchlist ticker search — the market lookup fallback).
//
// The reported defect: typing `cour` into the Watchlist card's combobox
// printed "No matching securities." — the picker filtered ONLY the tenant's
// stored securities, and a watchlist exists to follow a ticker you do NOT
// hold. This file pins the fallback's contract:
//
//   1. a STORED match short-circuits the market entirely (no probe);
//   2. one probe per settled query (600 ms debounce), and the pending row
//      withholds the no-match copy while the request is out;
//   3. the resolved row is one click: create → watch → both lists refetch →
//      the field clears;
//   4. `ambiguous` offers EVERY candidate with its currency, and only the
//      pick is stored (M143's never-guess rule);
//   5. a miss keeps "No matching securities." and writes nothing;
//   6. the probe never runs while the stored list is still loading;
//   7. the trade form's picker stays local-only (it is documented as the
//      existing-securities picker).
//
// Harness mirrors m103EmptyStateAction.test.tsx (real page, mocked domain
// APIs). Timers are REAL on purpose — a file-wide fake-timer freeze hangs
// @testing-library's waitFor (AGENTS §6), and the debounce is 600 ms.
import type { ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ApiError } from "../src/api/client";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";
import type { SecurityLookupResult } from "../src/api/securities";

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
    lookupSecurity: vi.fn(),
    createSecurity: vi.fn(),
    addWatchlistEntry: vi.fn(),
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
    // Rebuilt on the mock the way m103 does it: the real hook calls
    // `listAccounts` through accounts.ts's OWN module binding.
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
  };
});
vi.mock("../src/api/watchlist", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/watchlist")>();
  return {
    ...actual,
    getWatchlist: h.getWatchlist,
    addWatchlistEntry: h.addWatchlistEntry,
  };
});
vi.mock("../src/api/securities", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/securities")>();
  return {
    ...actual,
    listSecurities: h.listSecurities,
    lookupSecurity: h.lookupSecurity,
    createSecurity: h.createSecurity,
  };
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

/** Minimal EventSource stand-in (usePriceStream.test.ts). */
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

const WATCH_PLACEHOLDER = "Add a symbol to follow…";
const TRADE_PLACEHOLDER = "Search symbol or name…";
const WATCH_INPUT = `input[placeholder="${WATCH_PLACEHOLDER}"]`;

const watchInput = () => document.querySelector<HTMLInputElement>(WATCH_INPUT);
/** The picker input exists before any query resolves — wait for it, then type. */
async function typeInto(placeholder: string, value: string): Promise<HTMLInputElement> {
  const input = await screen.findByPlaceholderText(placeholder);
  fireEvent.change(input, { target: { value } });
  return input as HTMLInputElement;
}

/** The stored tenant securities: neither of them is a `cour*` listing. */
const STORED = [
  {
    id: 7,
    symbol: "RY.TO",
    name: "Royal Bank of Canada",
    kind: "stock",
    listing_currency: "CAD",
    exchange: "TSX",
  },
  {
    id: 9,
    symbol: "NVDA",
    name: "NVIDIA Corporation",
    kind: "stock",
    listing_currency: "USD",
    exchange: "NASDAQ",
  },
];

const COUR: SecurityLookupResult = {
  symbol: "COUR",
  name: "Coursera, Inc.",
  kind: "stock",
  listing_currency: "USD",
  exchange: "NASDAQ",
  exchange_display: "NasdaqGS",
  match: "direct",
  candidates: [],
};

const VGRO_COLLISION: SecurityLookupResult = {
  symbol: null,
  name: null,
  kind: null,
  listing_currency: null,
  exchange: null,
  exchange_display: null,
  match: "ambiguous",
  candidates: [
    {
      symbol: "VGRO",
      name: "Virtus Silvant Growth Opportuni",
      kind: "etf",
      listing_currency: "USD",
      exchange: "NYSEArca",
      exchange_display: "NYSEArca",
    },
    {
      symbol: "VGRO.TO",
      name: "VANGUARD GROWTH ETF PORTFOLIO",
      kind: "etf",
      listing_currency: "CAD",
      exchange: "TSX",
      exchange_display: "Toronto",
    },
  ],
};

/** ms the picker gives a settled query before it probes (docs/09). */
const DEBOUNCE = 600;
/** Comfortably past the debounce, for every "must NOT happen" assertion. */
const AFTER_DEBOUNCE = DEBOUNCE + 600;

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
  h.getPositions.mockResolvedValue({ items: [], total: 0 });
  h.getPortfolioValueHistory.mockResolvedValue({ events: [] });
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue(h.page(STORED));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  // Default: a miss (the honest "No matching securities." path). Tests that
  // pin a real answer override this — and an unexpected probe therefore
  // fails its assertion instead of throwing on an unconfigured mock.
  h.lookupSecurity.mockRejectedValue(
    new ApiError(404, "No market data available for symbol 'ZZZ'", "lookup_unavailable"),
  );
  h.createSecurity.mockRejectedValue(new Error("createSecurity: unexpected call"));
  h.addWatchlistEntry.mockResolvedValue(undefined);
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("watchlist ticker search — the market lookup fallback (M186)", () => {
  it("1. a stored match wins: the market is never asked", async () => {
    renderPage(<Investments />);
    await typeInto(WATCH_PLACEHOLDER, "ry");
    // The local row IS the answer, so it renders with no probe at all.
    await screen.findByTestId("security-option-RY.TO");
    await new Promise((r) => setTimeout(r, AFTER_DEBOUNCE));

    expect(h.lookupSecurity).not.toHaveBeenCalled();
    expect(screen.queryByTestId("security-market-searching")).toBeNull();
    expect(screen.queryByTestId("security-no-match")).toBeNull();
  });

  it("2. probes once per settled query, and the pending row withholds the no-match copy", async () => {
    h.lookupSecurity.mockImplementation(
      () =>
        new Promise<SecurityLookupResult>((resolve) => {
          window.setTimeout(() => resolve(COUR), DEBOUNCE);
        }),
    );
    renderPage(<Investments />);
    const input = await screen.findByPlaceholderText(WATCH_PLACEHOLDER);

    // Four keystrokes, no settle between them → ONE probe for the last value.
    for (const value of ["c", "co", "cou", "cour"]) {
      fireEvent.change(input, { target: { value } });
    }

    const pending = await screen.findByTestId(
      "security-market-searching",
      undefined,
      { timeout: 4000 },
    );
    expect(pending.textContent).toBe("Searching the market…");
    // While a request is out, the field does not claim there is no match.
    expect(screen.queryByTestId("security-no-match")).toBeNull();
    expect(h.lookupSecurity).toHaveBeenCalledTimes(1);
    expect(h.lookupSecurity).toHaveBeenCalledWith("COUR");

    await screen.findByTestId("security-market-option-COUR", undefined, { timeout: 4000 });
    expect(screen.queryByTestId("security-market-searching")).toBeNull();
    expect(h.lookupSecurity).toHaveBeenCalledTimes(1);
  });

  it("3. the resolved row's pick creates the security, watches it, and clears the field", async () => {
    h.lookupSecurity.mockResolvedValue(COUR);
    h.createSecurity.mockResolvedValue({
      id: 42,
      symbol: "COUR",
      name: "Coursera, Inc.",
      kind: "stock",
      listing_currency: "USD",
      exchange: "NASDAQ",
      dead_symbol: false,
      quote_miss_count: 0,
      dead_symbol_reason: null,
    });
    renderPage(<Investments />);
    await typeInto(WATCH_PLACEHOLDER, "cour");
    const row = await screen.findByTestId("security-market-option-COUR", undefined, {
      timeout: 4000,
    });
    // The row names the venue AND the currency (M143) and says where it came from.
    expect(row.textContent).toBe("COUR — Coursera, Inc. (NasdaqGS, USD) · from market");

    fireEvent.click(row);

    await waitFor(() => {
      expect(h.createSecurity).toHaveBeenCalledTimes(1);
      expect(h.createSecurity).toHaveBeenCalledWith({
        symbol: "COUR",
        name: "Coursera, Inc.",
        kind: "stock",
        listing_currency: "USD",
        exchange: "NASDAQ",
      });
      expect(h.addWatchlistEntry).toHaveBeenCalledWith(42);
    });
    // Both lists refetch so the new row is visible below and in the list.
    await waitFor(() => {
      expect(h.getWatchlist.mock.calls.length).toBeGreaterThan(1);
      expect(h.listSecurities.mock.calls.length).toBeGreaterThan(1);
    });
    // The picker cleared and closed itself on the pick.
    expect(watchInput()?.value).toBe("");
    expect(document.querySelector('[role="listbox"]')).toBeNull();
  });

  it("4. an ambiguous answer offers every candidate with its currency, storing only the pick", async () => {
    h.lookupSecurity.mockResolvedValue(VGRO_COLLISION);
    h.createSecurity.mockResolvedValue({
      id: 77,
      symbol: "VGRO.TO",
      name: "VANGUARD GROWTH ETF PORTFOLIO",
      kind: "etf",
      listing_currency: "CAD",
      exchange: "TSX",
      dead_symbol: false,
      quote_miss_count: 0,
      dead_symbol_reason: null,
    });
    renderPage(<Investments />);
    await typeInto(WATCH_PLACEHOLDER, "vgro");
    const usd = await screen.findByTestId("security-market-candidate-VGRO", undefined, {
      timeout: 4000,
    });
    const cad = screen.getByTestId("security-market-candidate-VGRO.TO");
    expect(usd.textContent).toBe(
      "VGRO — Virtus Silvant Growth Opportuni (NYSEArca, USD) · from market",
    );
    expect(cad.textContent).toBe(
      "VGRO.TO — VANGUARD GROWTH ETF PORTFOLIO (Toronto, CAD) · from market",
    );

    // Exactly ONE lookup happened, and the pick is applied directly (M143).
    fireEvent.click(cad);
    await waitFor(() => {
      expect(h.lookupSecurity).toHaveBeenCalledTimes(1);
      expect(h.createSecurity).toHaveBeenCalledWith({
        symbol: "VGRO.TO",
        name: "VANGUARD GROWTH ETF PORTFOLIO",
        kind: "etf",
        listing_currency: "CAD",
        exchange: "TSX",
      });
      expect(h.addWatchlistEntry).toHaveBeenCalledWith(77);
    });
  });

  it("5. a miss keeps \"No matching securities.\" and writes nothing", async () => {
    renderPage(<Investments />);
    await typeInto(WATCH_PLACEHOLDER, "zzzz");

    // The default mock is the 404 lookup_unavailable answer.
    await waitFor(
      () => {
        expect(h.lookupSecurity).toHaveBeenCalledWith("ZZZZ");
      },
      { timeout: 4000 },
    );
    await waitFor(
      () => {
        expect(screen.getByTestId("security-no-match").textContent).toBe(
          "No matching securities.",
        );
      },
      { timeout: 4000 },
    );
    expect(screen.queryByTestId("security-market-searching")).toBeNull();
    expect(screen.queryByTestId("security-market-option-ZZZZ")).toBeNull();
    expect(h.createSecurity).not.toHaveBeenCalled();
    expect(h.addWatchlistEntry).not.toHaveBeenCalled();
  });

  it("6. never probes while the stored list is still loading", async () => {
    h.listSecurities.mockReturnValue(new Promise(() => {}));
    renderPage(<Investments />);
    await typeInto(WATCH_PLACEHOLDER, "cour");
    await new Promise((r) => setTimeout(r, AFTER_DEBOUNCE));

    // `onSearchMarket` is wired only on `securities.isSuccess`, so an unknown
    // list can neither hide a stored row nor race a duplicate create.
    expect(h.lookupSecurity).not.toHaveBeenCalled();
  });

  it("7. the trade-form picker stays local-only", async () => {
    renderPage(<Investments />);
    await typeInto(TRADE_PLACEHOLDER, "zzzz");
    await new Promise((r) => setTimeout(r, AFTER_DEBOUNCE));

    expect(h.lookupSecurity).not.toHaveBeenCalled();
    expect(screen.getByTestId("security-no-match").textContent).toBe(
      "No matching securities.",
    );
  });
});

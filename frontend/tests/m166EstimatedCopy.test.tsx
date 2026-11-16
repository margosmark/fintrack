// M166 — the estimate honesty copy (docs/09 §Exposure honesty):
//
//   a slice allocated from a fund's OWN published sector mix is an estimate,
//   and the Exposure card says so: the rows it feeds are marked `est`, and a
//   note under the legend quotes the share and the dollar amount. A payload
//   without the field (older API) or one that reports zero renders no note.
//
// Harness mirrors m147UnclassifiedCopy.test.tsx: the real Investments page,
// the real query client, mocked domain APIs.
import type { ReactElement } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
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

const SECTOR_BREAKDOWN = {
  dimension: "sector",
  items: [
    {
      key: "Technology",
      market_value_cad: "381039.46",
      weight_pct: 26.37,
      estimated_cad: "20368.08",
      estimated: true,
    },
    {
      key: "Financial Services",
      market_value_cad: "290359.12",
      weight_pct: 20.1,
      estimated_cad: "5181.86",
      estimated: true,
    },
    {
      key: "Unclassified",
      market_value_cad: "174868.34",
      weight_pct: 12.1,
      estimated_cad: "0.00",
      estimated: false,
    },
  ],
  total_cad: "1444820.89",
  unclassified_cad: "174868.34",
  estimated_cad: "49757.41",
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
  h.getBreakdown.mockResolvedValue(SECTOR_BREAKDOWN);
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

async function openSectorTab() {
  renderPage(<Investments />);
  fireEvent.click(await screen.findByRole("tab", { name: "Sector" }));
  await screen.findByLabelText("Exposure breakdown table");
}

describe("M166 — the estimate honesty copy", () => {
  it("shows successful-empty dividend history distinctly from missing data", async () => {
    const suggestions = Object.assign([], {
      history: {
        pending: 0,
        unavailable: 0,
        empty: 1,
        stale: 0,
        as_of: "2026-10-13T12:00:00Z",
      },
    });
    h.listAccounts.mockResolvedValue(h.page([{ ...h.brokerage, type: "rrsp" }]));
    h.getDividendSuggestions.mockResolvedValue(suggestions);

    renderPage(<Investments />);

    expect(await screen.findByText(/no dividend events/)).toBeTruthy();
    expect(screen.getByText(/2026-10-13T12:00:00Z/)).toBeTruthy();
  });

  it("keeps pending, unavailable, and stale source state visible with an empty list", async () => {
    const suggestions = Object.assign([], {
      history: {
        pending: 1,
        unavailable: 2,
        empty: 0,
        stale: 1,
        as_of: "2026-10-10T12:00:00Z",
      },
    });
    h.listAccounts.mockResolvedValue(h.page([{ ...h.brokerage, type: "rrsp" }]));
    h.getDividendSuggestions.mockResolvedValue(suggestions);

    renderPage(<Investments />);

    expect(await screen.findByText(/1 dividend history refresh is pending/)).toBeTruthy();
    expect(screen.getByText(/2 dividend history sources are unavailable/)).toBeTruthy();
    expect(screen.getByText(/last-good history, if any, is retained/)).toBeTruthy();
    expect(screen.getByText(/1 dividend history source is stale.*2026-10-10T12:00:00Z/)).toBeTruthy();
  });

  it("M186: splits hard quote misses out of the unavailable count", async () => {
    // unavailable=2 where 1 is a hard quote miss: the transient sentence
    // counts only the remaining 1, and the miss gets its own honest copy
    // (retrying cannot help a symbol with no public quote).
    const suggestions = Object.assign([], {
      history: {
        pending: 0,
        unavailable: 2,
        empty: 0,
        stale: 0,
        quote_miss: 1,
        as_of: "2026-10-10T12:00:00Z",
      },
    });
    h.listAccounts.mockResolvedValue(h.page([{ ...h.brokerage, type: "rrsp" }]));
    h.getDividendSuggestions.mockResolvedValue(suggestions);

    renderPage(<Investments />);

    expect(await screen.findByText(/1 dividend history source is unavailable/)).toBeTruthy();
    expect(screen.getByText(/no public quote for the symbol \(possibly delisted\)/)).toBeTruthy();
    expect(screen.getByText(/automatic retries are paused/)).toBeTruthy();
    // The two unavailable rows are never double-reported as two unavailable.
    expect(screen.queryByText(/2 dividend history sources are unavailable/)).toBeNull();
  });

  it("M186: a header without quote_miss (older API) keeps the flat unavailable copy", async () => {
    const suggestions = Object.assign([], {
      history: {
        pending: 0,
        unavailable: 1,
        empty: 0,
        stale: 0,
        as_of: null,
      },
    });
    h.listAccounts.mockResolvedValue(h.page([{ ...h.brokerage, type: "rrsp" }]));
    h.getDividendSuggestions.mockResolvedValue(suggestions);

    renderPage(<Investments />);

    expect(await screen.findByText(/1 dividend history source is unavailable/)).toBeTruthy();
    expect(screen.queryByText(/no public quote/)).toBeNull();
  });

  it("quotes the estimated share, the dollar amount, and what stays exact", async () => {
    await openSectorTab();

    await screen.findByText(/published sector mix/);
    expect(screen.getByText(/≈3\.4% of this pie/)).toBeTruthy();
    expect(screen.getByText(/\$49,757\.41/)).toBeTruthy();
    expect(screen.getByText(/Company, country and asset class stay exact/)).toBeTruthy();
  });

  it("marks only the estimated rows in the legend table", async () => {
    await openSectorTab();

    const table = screen.getByLabelText("Exposure breakdown table");
    // two estimated sectors + one "est" inside the note itself
    expect(within(table).getAllByText("est")).toHaveLength(2);
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1]).queryByText("est")).toBeTruthy(); // Technology
    expect(within(rows[3]).queryByText("est")).toBeNull(); // Unclassified
  });

  it("an estimated row still states how much of it is an estimate", async () => {
    await openSectorTab();

    const table = screen.getByLabelText("Exposure breakdown table");
    const [technology] = within(table).getAllByText("est");
    expect(technology.getAttribute("title")).toBe(
      "$20,368.08 of this slice is allocated from the fund's own published sector mix.",
    );
  });

  it("renders no note when every row is sourced", async () => {
    h.getBreakdown.mockResolvedValue({
      ...SECTOR_BREAKDOWN,
      estimated_cad: "0.00",
      items: SECTOR_BREAKDOWN.items.map((row) => ({
        ...row,
        estimated_cad: "0.00",
        estimated: false,
      })),
    });

    await openSectorTab();

    expect(screen.queryByText(/published sector mix/)).toBeNull();
    expect(within(screen.getByLabelText("Exposure breakdown table")).queryByText("est")).toBeNull();
  });

  it("renders no note when the payload omits the fields (older API)", async () => {
    h.getBreakdown.mockResolvedValue({
      dimension: "sector",
      items: SECTOR_BREAKDOWN.items.map(({ estimated_cad: _c, estimated: _e, ...row }) => row),
      total_cad: SECTOR_BREAKDOWN.total_cad,
      unclassified_cad: SECTOR_BREAKDOWN.unclassified_cad,
    });

    await openSectorTab();

    expect(screen.queryByText(/published sector mix/)).toBeNull();
    expect(within(screen.getByLabelText("Exposure breakdown table")).queryByText("est")).toBeNull();
  });
});

// M129 — day delta + own-position impact, behaviour-first:
//
//   S1 the security page: the price card gains the daily delta vs the
//      previous close (and honestly shows NO delta line when there is no
//      prior close), the "Your position" impact card appears ONLY when the
//      user holds the security, and the SSE tick re-evaluates the
//      price-derived figures live — labeled "live" — while book cost never
//      moves;
//   S2 the positions table: Day Δ / Your gain (CAD) / Your gain % render the
//      SERVER's fields (no client arithmetic), the Σ row carries the
//      server's total unrealized, and an unknown-book row degrades to an
//      em dash instead of a fabricated zero.
//
// Harness mirrors m126PriceHistory.test.tsx: the real pages, the real query
// client, mocked domain APIs, a FakeEventSource.
import type { ReactElement } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";
import { SecurityDetail } from "../src/pages/SecurityDetail";

const h = vi.hoisted(() => {
  const user = (privacy: boolean) => ({
    id: "user-1",
    email: "owner@example.com",
    privacy_mode: privacy,
    currency_tracker_enabled: false,
    llm_provider: "openai",
    llm_base_url: null,
    llm_model: "gpt-4o-mini",
    encryption_required: false,
  });
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

  /** One positions row as the SERVER now sends it (docs/08 M129 block). */
  const position = (over: Record<string, unknown> = {}) => ({
    account_id: 1,
    account_name: "TFSA",
    security_id: 7,
    symbol: "MSFT",
    listing_currency: "USD",
    quantity: "35.2491",
    price_date: "2026-10-08",
    price: "517.42",
    price_source: "yfinance",
    price_age_days: 1,
    price_stale: false,
    book_cost_cad: "17432.11",
    spark: ["500", "505", "510", "517.42"],
    market_value_native: "18238.60",
    market_value_cad: "18239.44",
    fx_rate_to_cad: "1.4",
    previous_close_native: "510.00",
    day_change_native: "7.42",
    day_change_pct: 1.45,
    day_change_cad: "366.17",
    unrealized_gain_cad: "807.33",
    unrealized_gain_native: "590.22",
    unrealized_gain_pct: 4.63,
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
    getSecurityOverview: vi.fn(),
    getPriceHistory: vi.fn(),
    user,
    brokerage,
    page,
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
  return {
    ...actual,
    listSecurities: h.listSecurities,
    getSecurityOverview: h.getSecurityOverview,
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

function wrap(ui: ReactElement, path: string, entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <ToastProvider>
            <AuthProvider>
              <Routes>
                <Route path={path} element={ui} />
                <Route path="*" element={ui} />
              </Routes>
            </AuthProvider>
          </ToastProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const renderSecurity = () => wrap(<SecurityDetail />, "/securities/:securityId", "/securities/7");
const renderInvestments = () => wrap(<Investments />, "*", "/investments");

async function openStream(): Promise<void> {
  await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0));
  act(() => {
    FakeEventSource.instances[0].onopen?.(new Event("open"));
  });
}

/** Push one SSE payload through the live connection. */
function emit(payload: unknown): void {
  act(() => {
    FakeEventSource.instances[0].onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  });
}

const QUOTE = (over: Record<string, unknown> = {}) => ({
  security_id: 7,
  symbol: "MSFT",
  price: "517.42",
  date: "2026-10-08",
  source: "yfinance",
  age_days: 1,
  stale: false,
  ...over,
});

/** The overview payload the server sends after the M129 wire extension. */
const OVERVIEW = (over: Record<string, unknown> = {}) => ({
  security: {
    id: 7,
    symbol: "MSFT",
    name: "Microsoft Corporation",
    kind: "stock",
    listing_currency: "USD",
    exchange: "NMS",
  },
  latest: {
    date: "2026-10-08",
    price: "517.42",
    source: "yfinance",
    age_days: 1,
    stale: false,
    previous_close: "510.00",
    day_change_native: "7.42",
    day_change_pct: 1.45,
  },
  stats: { high_52w: "520.00", low_52w: "380.00", change_pct: 5.2, cached_days: 300 },
  history: [
    { date: "2026-10-07", price: "510.00" },
    { date: "2026-10-08", price: "517.42" },
  ],
  positions: [h.position()],
  totals: {
    quantity: "35.2491",
    book_cost_cad: "17432.11",
    market_value_cad: "18239.44",
    unrealized_gain_cad: "807.33",
    unrealized_gain_native: "590.22",
    unrealized_gain_pct: 4.63,
    day_change_cad: "366.17",
  },
  watchlisted: false,
  fundamentals: null,
  holdings: null,
  recent_trades: [],
  ...over,
});

beforeEach(() => {
  FakeEventSource.instances = [];
  h.restoreSession.mockResolvedValue({
    access_token: "token",
    token_type: "bearer",
    user: h.user(false),
  });
  h.me.mockResolvedValue(h.user(false));
  h.listAccounts.mockResolvedValue(h.page([h.brokerage]));
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  h.getExposure.mockResolvedValue({ items: [], total: 0 });
  h.getBreakdown.mockResolvedValue({
    dimension: "sector",
    items: [],
    total_cad: "0",
    unclassified_cad: "0",
  });
  h.getPriceHistory.mockResolvedValue({
    security_id: 7,
    from: "2026-09-08",
    to: "2026-10-08",
    granularity: "day",
    requested_granularity: "day",
    count: 2,
    items: [
      { date: "2026-10-07", price: "510.00" },
      { date: "2026-10-08", price: "517.42" },
    ],
  });
  h.getPositions.mockResolvedValue({
    items: [h.position()],
    total: 1,
    total_unrealized_cad: "807.33",
  });
  h.getSecurityOverview.mockResolvedValue(OVERVIEW());
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("S1 — the security page's delta + impact (M129)", () => {
  it("renders the daily delta from the SERVER's fields, coloured and signed", async () => {
    renderSecurity();
    const delta = await screen.findByTestId("day-delta");
    expect(delta.textContent).toContain("+7.42");
    expect(delta.textContent).toContain("(+1.45%)");
    expect(delta.textContent).toContain("▲");
    // At rest the numbers are the server's — no "live" claim.
    expect(delta.querySelector(".text-gain")).not.toBeNull();
    expect(delta.textContent).not.toContain("live");
  });

  it("withholds the delta line entirely when there is no previous close", async () => {
    h.getSecurityOverview.mockResolvedValue(
      OVERVIEW({
        latest: {
          date: "2026-10-08",
          price: "517.42",
          source: "yfinance",
          age_days: 1,
          stale: false,
          previous_close: null,
          day_change_native: null,
          day_change_pct: null,
        },
      }),
    );
    renderSecurity();
    await screen.findByText("517.42 USD");
    // A first-day listing states no move at all — never a fabricated 0.00%.
    expect(document.querySelector('[data-testid="day-delta"]')).toBeNull();
  });

  it("shows the impact card with the server's book/market/gain numbers", async () => {
    renderSecurity();
    const card = await screen.findByTestId("impact-card");
    const headline = card.querySelector('[data-testid="impact-headline"]');
    expect(headline?.textContent).toContain("You hold");
    expect(headline?.textContent).toContain("35.2491");
    expect(headline?.textContent).toContain("$17,432.11"); // book (server)
    expect(headline?.textContent).toContain("$18,239.44"); // market (server)
    expect(headline?.textContent).toContain("+$807.33"); // gain (server)
    expect(headline?.textContent).toContain("(+4.63%)");
    // Today's impact on YOUR shares, from the server's day_change_cad.
    const today = card.querySelector('[data-testid="impact-day"]');
    expect(today?.textContent).toContain("+$366.17");
  });

  it("suppresses the impact card when the user holds nothing", async () => {
    h.getSecurityOverview.mockResolvedValue(
      OVERVIEW({
        positions: [],
        totals: {
          quantity: "0",
          book_cost_cad: null,
          market_value_cad: null,
          unrealized_gain_cad: null,
          unrealized_gain_native: null,
          unrealized_gain_pct: null,
          day_change_cad: null,
        },
      }),
    );
    renderSecurity();
    await screen.findByText("517.42 USD");
    // Never a fake zero row: the card is simply absent (docs/09).
    expect(document.querySelector('[data-testid="impact-card"]')).toBeNull();
    expect(screen.getByText("Not held — watchlist only")).toBeTruthy();
  });

  it("re-evaluates the impact LIVE on a tick, labels it, and never moves book cost", async () => {
    renderSecurity();
    await screen.findByTestId("impact-card");
    await openStream();

    // A new close arrives: 530.00 (up from 517.42).
    emit([QUOTE({ price: "530.00" })]);

    await waitFor(() => {
      const card = document.querySelector('[data-testid="impact-card"]');
      expect(card?.textContent).toContain("live");
    });
    const card = document.querySelector('[data-testid="impact-card"]')!;
    const headline = card.querySelector('[data-testid="impact-headline"]')!;
    // Market value re-evaluated: 35.2491 × 530 × 1.4 ≈ $26,154.83.
    expect(headline.textContent).toContain("$26,154.83");
    // …and so did the unrealized gain: 26,154.83 − 17,432.11 = $8,722.72.
    expect(headline.textContent).toContain("+$8,722.72");
    // Book cost is history, not a quote — it does not move on a tick.
    expect(headline.textContent).toContain("$17,432.11");
    // The delta line re-evaluated too: 530 − 510 = +20.00 (+3.92%).
    const delta = document.querySelector('[data-testid="day-delta"]')!;
    expect(delta.textContent).toContain("+20.00");
    expect(delta.textContent).toContain("(+3.92%)");
    expect(delta.textContent).toContain("live");
  });

  it("claims nothing live for a payload that did not move", async () => {
    renderSecurity();
    await screen.findByTestId("impact-card");
    await openStream();

    emit([QUOTE()]); // identical to the cached quote
    await act(async () => {});

    expect(document.querySelector('[data-testid="impact-card"]')!.textContent).not.toContain(
      "live",
    );
    expect(screen.queryByText("$26,154.83")).toBeNull();
  });
});

describe("S2 — the positions table's delta columns (M129)", () => {
  it("renders Day Δ / Your gain (CAD) / Your gain % from the SERVER's fields", async () => {
    renderInvestments();
    await screen.findByText("MSFT");
    // Day Δ: the server's day_change_pct, signed and coloured.
    expect(screen.getByText("+1.45%")).toBeTruthy();
    // Your gain (CAD) + the percent beside it (the <sm fold). The same
    // figure appears in the Σ row — the server's total agrees by
    // construction — so match every occurrence.
    expect(screen.getAllByText("+$807.33").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("+4.63%").length).toBeGreaterThanOrEqual(1);
  });

  it("carries the server's total unrealized in the Σ row", async () => {
    renderInvestments();
    const totalRow = await screen.findByTestId("positions-total-row");
    expect(totalRow.textContent).toContain("Total unrealized (CAD)");
    expect(totalRow.textContent).toContain("+$807.33");
  });

  it("degrades an unknown-book row to an em dash, never a zero", async () => {
    h.getPositions.mockResolvedValue({
      items: [h.position({ book_cost_cad: null, unrealized_gain_cad: null, unrealized_gain_pct: null })],
      total: 1,
      total_unrealized_cad: null,
    });
    renderInvestments();
    const gainCell = await waitFor(() => {
      const cells = Array.from(document.querySelectorAll("td")).filter(
        (td) => td.textContent === "—",
      );
      expect(cells.length).toBeGreaterThan(0);
      return cells[0];
    });
    expect(gainCell).toBeTruthy();
    // The Σ row refuses too.
    const totalRow = await screen.findByTestId("positions-total-row");
    expect(totalRow.textContent).toContain("Total unrealized (CAD)");
    expect(totalRow.textContent).toContain("—");
  });

  it("keeps the source/age diagnostics available at >=sm and hides them below it", async () => {
    renderInvestments();
    await screen.findByText("MSFT");
    // jsdom cannot apply media queries, so the CONTRACT is the class pair:
    // the diagnostics header and the gain-% header hide below sm only, and
    // the delta columns never do (the live probe measures the pixels).
    const headers = Array.from(document.querySelectorAll("th")).map((th) => th.textContent ?? "");
    const diagnostics = document.querySelectorAll("th")[headers.indexOf("Source · age")];
    const gainPct = document.querySelectorAll("th")[headers.indexOf("Your gain %")];
    const dayDelta = document.querySelectorAll("th")[headers.indexOf("Day Δ")];
    expect(diagnostics.className).toContain("hidden");
    expect(diagnostics.className).toContain("sm:table-cell");
    expect(gainPct.className).toContain("hidden");
    expect(gainPct.className).toContain("sm:table-cell");
    expect(dayDelta.className).not.toContain("hidden");
  });
});

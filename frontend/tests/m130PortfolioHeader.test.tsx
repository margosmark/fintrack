// M130 — the total-portfolio header (docs/09 §Portfolio header, docs/08
// §Portfolio summary / §Portfolio value history), behaviour-first:
//
//   H1 the headline, the day change and the range sentence are all RENDERS of
//      the server's payload: the Σ row of the positions table and the header
//      card print the same numbers because they read the same fields — the
//      anti-fabrication identity, asserted as text equality;
//   H2 the day change names the real prior market day ("vs Oct 5 close"), is
//      withheld when the server has no baseline, and says so when the rows
//      disagree about which day that was;
//   H3 a stream tick re-evaluates the headline from the server's own
//      primitives and labels it "live" — cash never moves with a quote;
//   H4 the range selector drives ONE number: the sentence above the chart is
//      `stats` from the response for THAT window, and the chart's data table
//      holds that window's rows.
//   H5 the range sentence is honest about a missing percent: a window that
//      STARTED at zero has a real CAD move and no denominator, so the move
//      always renders and the pct appends only when the server could divide —
//      the line withholds only when the move itself is absent.
//
// Harness mirrors m129DayDelta.test.tsx: the real page, the real query
// client, mocked domain APIs, a FakeEventSource.
import type { ReactElement } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";
import { formatCad } from "../src/lib/format";

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
    user,
    brokerage,
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

function wrap(ui: ReactElement, entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[entry]}>
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

const renderInvestments = () => wrap(<Investments />, "/investments");

const BUCKETS = [
  {
    bucket_end: "2025-01-31",
    cash_cad: "4000.00",
    positions_cad: "5000.00",
    value_cad: "9000.00",
    estimated: false,
    valued: true,
  },
  {
    bucket_end: "2025-02-28",
    cash_cad: "4000.00",
    positions_cad: "5400.00",
    value_cad: "9400.00",
    estimated: false,
    valued: true,
  },
  {
    bucket_end: "2025-03-31",
    cash_cad: "4000.00",
    positions_cad: "5900.00",
    value_cad: "9900.00",
    estimated: false,
    valued: true,
  },
];

const ALL_STATS = {
  from_bucket: "2025-01-31",
  to_bucket: "2025-03-31",
  from_value_cad: "9000.00",
  to_value_cad: "9900.00",
  change_cad: "900.00",
  change_pct: 10.0,
};

/** A different window → a different answer, so the sentence can be pinned. */
const SIX_MONTH_STATS = {
  from_bucket: "2024-10-04",
  to_bucket: "2025-03-31",
  from_value_cad: "7000.00",
  to_value_cad: "9900.00",
  change_cad: "2900.00",
  change_pct: 41.43,
};

function history(over: Record<string, unknown> = {}) {
  return {
    account_ids: [1, 2],
    from: "2025-01-01",
    to: "2025-03-31",
    granularity: "month",
    requested_granularity: "month",
    currency: "CAD",
    native_currency: "CAD",
    min_event_value: "1000.00",
    has_data: true,
    buckets: BUCKETS,
    events: [],
    stats: ALL_STATS,
    ...over,
  };
}

const secondRow = () =>
  h.position({
    account_id: 2,
    account_name: "RRSP",
    security_id: 8,
    symbol: "BBB",
    quantity: "200",
    price: "20.00",
    previous_close_native: "18.00",
    market_value_native: "4000.00",
    market_value_cad: "4000.00",
    day_change_native: "2.00",
    day_change_pct: 11.11,
    day_change_cad: "400.00",
    book_cost_cad: "3600.00",
    unrealized_gain_cad: "400.00",
    unrealized_gain_native: "400.00",
    unrealized_gain_pct: 11.11,
    spark: ["18", "19", "20"],
  });

function positionsPayload(over: Record<string, unknown> = {}) {
  return {
    items: [h.position(), secondRow()],
    total: 2,
    total_unrealized_cad: "1400.00",
    total_value_cad: "14000.00",
    cash_cad: "0.00",
    portfolio_value_cad: "14000.00",
    total_day_change_cad: "1400.00",
    total_day_change_pct: 11.11,
    day_change_as_of: "2026-10-05",
    day_change_mixed_baseline: false,
    ...over,
  };
}

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

beforeEach(() => {
  FakeEventSource.instances = [];
  h.restoreSession.mockResolvedValue({
    access_token: "token",
    token_type: "bearer",
    user: h.user,
  });
  h.me.mockResolvedValue(h.user);
  h.listAccounts.mockResolvedValue({ items: [h.brokerage], total: 1, limit: 200, offset: 0 });
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
  h.getPositions.mockResolvedValue(positionsPayload());
  h.getPortfolioValueHistory.mockResolvedValue(history());
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("H1 — one source: the header and the Σ row read the same payload", () => {
  it("renders the server's portfolio value, day change and range stats", async () => {
    renderInvestments();
    const value = await screen.findByTestId("portfolio-value");
    expect(value.textContent).toContain(formatCad("14000.00"));

    const day = await screen.findByTestId("portfolio-day-change");
    expect(day.textContent).toContain("+");
    expect(day.textContent).toContain("+$1,400.00");
    expect(day.textContent).toContain("(+11.11%)");
    // The real market day the baseline came from — never "yesterday".
    expect(day.textContent).toContain("vs Oct 5 close");
    // At rest the numbers are the server's: no "live" claim anywhere in the
    // headline (the badge only appears while a stream tick rides the payload).
    expect(value.textContent ?? "").not.toContain("live");
    expect(screen.queryByText("live")).toBeNull();

    const stats = await screen.findByTestId("portfolio-range-stats");
    expect(stats.textContent).toContain("Over all time:");
    expect(stats.textContent).toContain("+$900.00");
    expect(stats.textContent).toContain("(+10.00%)");
    expect(stats.textContent).toContain("Jan 31 → Mar 31");
  });

  it("prints the SAME holdings total in the Σ row and in the header", async () => {
    renderInvestments();
    const value = await screen.findByTestId("portfolio-value");
    const row = await screen.findByTestId("positions-total-row");
    const headerTotal = value.textContent?.replace(/\s+/g, " ").trim() ?? "";
    const sigmaTotal = row.textContent?.match(/\$14,000\.00/)?.[0] ?? "";
    // Both cells are `formatCad(total_value_cad)` — one payload, one number.
    expect(headerTotal).toContain(formatCad("14000.00"));
    expect(sigmaTotal).toBe(formatCad("14000.00"));
    expect(row.textContent).toContain("Total market value (CAD)");
    expect(row.textContent).toContain("+11.11%");
    expect(row.textContent).toContain("Total day change");
  });

  it("shows the cash line only when the cash leg is non-zero", async () => {
    renderInvestments();
    await screen.findByTestId("portfolio-value");
    expect(screen.queryByTestId("portfolio-cash")).toBeNull();

    h.getPositions.mockResolvedValue(
      positionsPayload({
        cash_cad: "-3872.72",
        portfolio_value_cad: "10127.28",
      }),
    );
    // Re-render with the new payload (the query caches, so mount again).
    const second = renderInvestments();
    const cash = await screen.findByTestId("portfolio-cash");
    expect(cash.textContent).toContain("Cash in investment accounts");
    expect(cash.textContent).toContain("-$3,872.72");
    // The headline is holdings + cash, server-computed.
    const value = screen.getAllByTestId("portfolio-value");
    expect(value[value.length - 1].textContent).toContain(formatCad("10127.28"));
    second.unmount();
  });
});

describe("H2 — the day change is honest about its baseline", () => {
  it("withholds the line entirely when the server has no baseline", async () => {
    h.getPositions.mockResolvedValue(
      positionsPayload({
        total_day_change_cad: null,
        total_day_change_pct: null,
        day_change_as_of: null,
      }),
    );
    renderInvestments();
    await screen.findByTestId("portfolio-value");
    expect(screen.queryByTestId("portfolio-day-change")).toBeNull();
    // The Σ row is honest too: em dash, never a fabricated 0.00%.
    const row = await screen.findByTestId("positions-total-row");
    expect(row.textContent).toContain("—");
  });

  it("says the rows closed on different days instead of naming one", async () => {
    h.getPositions.mockResolvedValue(
      positionsPayload({ day_change_mixed_baseline: true }),
    );
    renderInvestments();
    const day = await screen.findByTestId("portfolio-day-change");
    expect(day.textContent).toContain("vs Oct 5 close");
    expect(day.textContent).toContain("some holdings last closed earlier");
  });

  it("withholds the whole headline's day claim for an empty portfolio", async () => {
    h.getPositions.mockResolvedValue(
      positionsPayload({
        items: [],
        total: 0,
        total_value_cad: "0.00",
        portfolio_value_cad: "0.00",
        total_day_change_cad: null,
        total_day_change_pct: null,
        day_change_as_of: null,
      }),
    );
    h.getPortfolioValueHistory.mockResolvedValue(history({ has_data: false, buckets: [], stats: {
      from_bucket: null,
      to_bucket: null,
      from_value_cad: null,
      to_value_cad: null,
      change_cad: null,
      change_pct: null,
    } }));
    renderInvestments();
    await screen.findByTestId("portfolio-value");
    expect(screen.queryByTestId("portfolio-day-change")).toBeNull();
    // Three-part empty state for the chart, not a flat $0 line.
    expect(await screen.findByText("No value history yet")).toBeTruthy();
  });
});

describe("H3 — the live tick", () => {
  it("re-evaluates the headline from server primitives and labels it live", async () => {
    renderInvestments();
    await screen.findByTestId("portfolio-value");
    await openStream();

    // The server payload that was on screen last, now carrying a moved quote.
    h.getPositions.mockResolvedValue(
      positionsPayload({
        livePatchedAt: Date.now(),
        items: [
          h.position({ price: "110.00" }),
          secondRow(),
        ],
      }),
    );
    emit([
      {
        security_id: 7,
        symbol: "AAA",
        price: "110.00",
        date: "2026-10-09",
        source: "yfinance",
        age_days: 0,
        stale: false,
      },
    ]);
    await waitFor(() => {
      const value = screen.getByTestId("portfolio-value");
      // 100 × 110 + 200 × 20 = 15,000 — from quantity × patched price × rate.
      expect(value.textContent).toContain(formatCad("15000.00"));
      expect(value.textContent).toContain("live");
    });
    const day = screen.getByTestId("portfolio-day-change");
    // 100 × (110−90) + 200 × (20−18) = 2,400 against the SERVER's baseline.
    expect(day.textContent).toContain("2,400.00");
    expect(day.textContent).toContain("(+19.05%)");
    // The baseline date is the server's, untouched by the tick.
    expect(day.textContent).toContain("vs Oct 5 close");
  });
});

describe("H4 — one range, one number", () => {
  it("re-requests the window and the sentence follows THAT response's stats", async () => {
    h.getPortfolioValueHistory.mockImplementation(
      async (params: { from?: string; to?: string } = {}) =>
        params.from === undefined
          ? (history() as never)
          : (history({
              buckets: BUCKETS.slice(1),
              stats: SIX_MONTH_STATS,
            }) as never),
    );
    renderInvestments();
    const allTime = await screen.findByTestId("portfolio-range-stats");
    expect(allTime.textContent).toContain("Over all time:");
    expect(allTime.textContent).toContain("+$900.00");

    fireEvent.click(screen.getByRole("button", { name: "6M" }));
    const sixMonths = await screen.findByTestId("portfolio-range-stats");
    await waitFor(() => expect(sixMonths.textContent).toContain("Over 6 months:"));
    expect(sixMonths.textContent).toContain("+$2,900.00");
    expect(sixMonths.textContent).toContain("(+41.43%)");

    const call = h.getPortfolioValueHistory.mock.calls.at(-1)?.[0] as { from?: string };
    expect(call.from).toBeTruthy();
    expect(screen.getByRole("button", { name: "6M" }).getAttribute("aria-pressed")).toBe("true");

    // The chart's data-table fallback holds THAT window's rows, by
    // construction — same payload, same `points` array. The 6M response
    // shipped 2 buckets (the `All` one shipped 3), so the table follows the
    // selector instead of keeping a second range state of its own.
    await screen.findByText("Portfolio value over time");
    const rows = document.querySelectorAll("details table tbody tr");
    expect(rows.length).toBe(2);
  });
});

describe("H5 — the move renders even when the percent cannot be divided", () => {
  it("prints the CAD move alone for a window that started at zero", async () => {
    // 6daed91: every new portfolio starts at 0.00 — a real CAD move with NO
    // denominator. Pre-fix the sentence collapsed to "—" whenever pct was
    // null, withholding a number the server did give us.
    h.getPortfolioValueHistory.mockResolvedValue(
      history({
        stats: { ...ALL_STATS, from_value_cad: "0.00", change_pct: null },
      }),
    );
    renderInvestments();
    const stats = await screen.findByTestId("portfolio-range-stats");
    expect(stats.textContent).toContain("Over all time:");
    // The move renders …
    expect(stats.textContent).toContain("+$900.00");
    // … the pct appends only when the server could divide: no "(…%)" ghost
    // next to it, and no em-dash withholding the whole line.
    expect(stats.textContent).not.toContain("%");
    expect(stats.textContent).not.toContain("—");
  });

  it("still withholds the line when the move itself is absent", async () => {
    // The honest-null side of the same contract: no change_cad → "—", never a
    // fabricated 0.00.
    h.getPortfolioValueHistory.mockResolvedValue(
      history({
        stats: {
          from_bucket: "2025-01-31",
          to_bucket: "2025-03-31",
          from_value_cad: "9000.00",
          to_value_cad: "9000.00",
          change_cad: null,
          change_pct: null,
        },
      }),
    );
    renderInvestments();
    const stats = await screen.findByTestId("portfolio-range-stats");
    expect(stats.textContent).toContain("Over all time:");
    expect(stats.textContent).toContain("—");
    expect(stats.textContent).not.toContain("$0.00");
  });
});

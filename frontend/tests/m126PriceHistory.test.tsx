// M126 — the three price-history asks, behaviour-first:
//
//   L1 the SSE stream patches a POSITIONS row's quote client-side (and only
//      the quote — market value stays server-owned), the "totals catch up"
//      note appears while the patch is ahead of the last server payload, and
//      privacy mode opens no stream at all, so nothing can be patched;
//   L2 a positions row expands INLINE into the real price-history chart: the
//      table alone costs ZERO history requests, expanding costs exactly one,
//      and a security with no closes says so instead of drawing a blank line;
//   L3 the range control is a labelled group of aria-pressed buttons —
//      keyboard-reachable, seven options, and choosing one re-fetches with
//      the documented `from`.
//
// Harness mirrors priceStaleStream.test.tsx: the real Investments page, the
// real query client, mocked domain APIs, a FakeEventSource.
import type { ReactElement } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";

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
  const position = (over: Record<string, unknown> = {}) => ({
    account_id: 1,
    account_name: "TFSA",
    security_id: 7,
    symbol: "MSFT",
    listing_currency: "USD",
    quantity: "10",
    price_date: "2026-10-09",
    price: "100.00",
    price_source: "yfinance",
    price_age_days: 0,
    price_stale: false,
    book_cost_cad: "1000.00",
    // 30 real closes: the sparkline reads this, NOT a history request.
    spark: Array.from({ length: 30 }, (_, i) => String(90 + i)),
    market_value_native: "1000.00",
    market_value_cad: "1350.00",
    ...over,
  });
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
  price: "101.00",
  date: "2026-10-09",
  source: "yfinance",
  age_days: 0,
  stale: false,
  ...over,
});

/**
 * `YYYY-MM-DD` in the browser's local calendar — the rule `rangeWindow` uses.
 */
function iso(d: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/**
 * today − `months` calendar months: the 1M/3M/… window docs/12 pins.
 *
 * Derived from the CLOCK at assertion time, never written as a date literal.
 * The literal `"2026-09-09"` this file used to assert (and to serve as the
 * server fixture's `from`) is true only while today is 2026-10-09 — it went
 * red for everyone at 00:00 EDT on 2026-10-10, taking the web gate with it.
 * It is also re-implemented here instead of imported from `src/lib/chartRange`
 * on purpose: an expectation computed by the code under test pins nothing.
 */
function monthsBack(months: number, now: Date = new Date()): string {
  return iso(new Date(now.getFullYear(), now.getMonth() - months, now.getDate()));
}

function history(count: number) {
  return {
    security_id: 7,
    // Payload only — the server's own window, whatever the page asked for.
    from: "2026-09-09",
    to: "2026-10-09",
    granularity: "day",
    requested_granularity: "day",
    count,
    items: Array.from({ length: count }, (_, i) => ({
      date: `2026-09-${String((i % 28) + 1).padStart(2, "0")}`,
      price: String(90 + i),
    })),
  };
}

beforeEach(() => {
  FakeEventSource.instances = [];
  h.restoreSession.mockResolvedValue({
    access_token: "token",
    token_type: "bearer",
    user: h.user(false),
  });
  h.me.mockResolvedValue(h.user(false));
  h.listAccounts.mockResolvedValue(h.page([h.brokerage]));
  h.listCategories.mockResolvedValue(h.page([]));
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.getExposure.mockResolvedValue({ items: [], total: 0 });
  h.getBreakdown.mockResolvedValue({
    dimension: "sector",
    items: [],
    total_cad: "0",
    unclassified_cad: "0",
  });
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  h.getPositions.mockResolvedValue({ items: [h.position()], total: 1 });
  h.getPriceHistory.mockResolvedValue(history(30));
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("L1 — the stream patches a positions row's quote (M126)", () => {
  it("moves the price cell, leaves server-owned valuation alone, and says so", async () => {
    renderPage(<Investments />);
    await openStream();

    // First tick: no invalidation (baseline), but the quote patch lands NOW.
    emit([QUOTE()]);
    await waitFor(() =>
      expect(document.querySelector('[data-live-tick="true"]')).not.toBeNull(),
    );

    // The QUOTE moved…
    expect(await screen.findByText("101.00 USD")).toBeTruthy();
    // …the server-owned valuation did NOT (it is not derivable client-side):
    // market value is still the server's 1350.00, book cost still 1000.00.
    expect(screen.getByText("$1,350.00")).toBeTruthy();
    expect(screen.getByText("$1,000.00")).toBeTruthy();
    // …and the card says totals are still the server's to move.
    expect(document.querySelector('[data-testid="positions-live-note"]')).not.toBeNull();
    // The SSE chip stays the ONLY statement about connection state.
    expect(screen.getByText("Stream connected")).toBeTruthy();
  });

  it("does not patch or claim freshness for a payload that did not change", async () => {
    renderPage(<Investments />);
    await openStream();

    emit([QUOTE({ price: "100.00" })]); // identical to the cached row
    await act(async () => {});

    expect(document.querySelector('[data-live-tick="true"]')).toBeNull();
    expect(document.querySelector('[data-testid="positions-live-note"]')).toBeNull();
    expect(screen.getByText("100.00 USD")).toBeTruthy();
  });

  it("patches the row behind an explicit account filter too (one key family)", async () => {
    renderPage(<Investments accountId={1} />);
    await openStream();
    emit([QUOTE()]);
    await waitFor(() => expect(screen.getByText("101.00 USD")).toBeTruthy());
  });

  it("privacy mode opens no stream at all, so nothing can be patched", async () => {
    h.restoreSession.mockResolvedValue({
      access_token: "token",
      token_type: "bearer",
      user: h.user(true),
    });
    h.me.mockResolvedValue(h.user(true));

    renderPage(<Investments />);
    await act(async () => {});

    expect(FakeEventSource.instances).toHaveLength(0);
    expect(screen.getByText("100.00 USD")).toBeTruthy();
    expect(document.querySelector('[data-testid="positions-live-note"]')).toBeNull();
    expect(h.getPriceHistory).not.toHaveBeenCalled();
  });
});

describe("L2 — per-holding history on the positions table (M126)", () => {
  it("renders every sparkline from the positions payload without one extra request", async () => {
    renderPage(<Investments />);
    await waitFor(() => expect(screen.getByText("MSFT")).toBeTruthy());

    // 30 closes came with the row; the history endpoint is never touched.
    expect(h.getPriceHistory).not.toHaveBeenCalled();
    // The chevron button is an SVG too — the SPARKLINE is the polyline.
    const sparkline = document.querySelector("tbody polyline");
    expect(sparkline).not.toBeNull();
    const coordinates = (sparkline?.getAttribute("points") ?? "").trim().split(/\s+/);
    expect(coordinates).toHaveLength(30);
  });

  it("expanding a row fetches that security's history once and draws the chart", async () => {
    renderPage(<Investments />);
    const toggle = await screen.findByRole("button", { name: "Show MSFT price history" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    h.getPriceHistory.mockResolvedValue(history(30));
    fireEvent.click(toggle);

    await waitFor(() => expect(h.getPriceHistory).toHaveBeenCalledTimes(1));
    const call = h.getPriceHistory.mock.calls[0][0];
    expect(call.securityId).toBe(7);
    // Default range for a held security is 1M: `today − 1 calendar month`
    // (local calendar), `to` = today, bucketed by day. All three come off the
    // clock — a date literal here is what turned the gate red at midnight.
    expect(call.from).toBe(monthsBack(1));
    expect(call.to).toBe(iso());
    expect(call.granularity).toBe("day");

    await screen.findByTestId("position-history-row");
    const frame = document.querySelector('[data-testid="position-history-row"] [role="img"]');
    expect(frame).not.toBeNull();
    expect(frame?.getAttribute("aria-label") ?? "").toContain("MSFT price history");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  it("a security with NO history says so — no fake blank chart", async () => {
    h.getPriceHistory.mockResolvedValue(history(0));
    renderPage(<Investments />);
    const toggle = await screen.findByRole("button", { name: "Show MSFT price history" });
    fireEvent.click(toggle);

    await screen.findByTestId("position-history-row");
    await screen.findByText(/No price history yet/);
    expect(
      document.querySelector('[data-testid="position-history-row"] [role="img"]'),
    ).toBeNull();
  });

  it("caps the expanded panel to the viewport — the table scrolls, the range control must not be off-screen", async () => {
    renderPage(<Investments />);
    const toggle = await screen.findByRole("button", { name: "Show MSFT price history" });
    fireEvent.click(toggle);
    const row = await screen.findByTestId("position-history-row");
    const cell = row.querySelector("td");
    expect(cell).not.toBeNull();
    // The cell spans a ~1400px-wide horizontally-scrollable table; without the
    // cap its right-aligned range control sits off-screen at 390/412 and the
    // only way to reach it is scrolling the TABLE (docs/09 §Range control).
    expect(cell?.querySelector('div[class*="max-w-[calc(100vw"]')).not.toBeNull();
  });

  it("collapsing does not refetch — the range result is cached by key", async () => {
    renderPage(<Investments />);
    const toggle = await screen.findByRole("button", { name: "Show MSFT price history" });
    fireEvent.click(toggle);
    await waitFor(() => expect(h.getPriceHistory).toHaveBeenCalledTimes(1));

    fireEvent.click(await screen.findByRole("button", { name: "Hide MSFT price history" }));
    await waitFor(() =>
      expect(document.querySelector('[data-testid="position-history-row"]')).toBeNull(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Show MSFT price history" }));
    await screen.findByTestId("position-history-row");
    expect(h.getPriceHistory).toHaveBeenCalledTimes(1);
  });
});

describe("L3 — the range control (M126)", () => {
  it("is a labelled group of seven aria-pressed toggles", async () => {
    renderPage(<Investments />);
    const toggle = await screen.findByRole("button", { name: "Show MSFT price history" });
    fireEvent.click(toggle);

    const group = await screen.findByRole("group", { name: /MSFT price history range/ });
    const buttons = Array.from(group.querySelectorAll("button"));
    expect(buttons).toHaveLength(7);
    expect(buttons.map((b) => b.textContent)).toEqual([
      "1M",
      "3M",
      "6M",
      "YTD",
      "1Y",
      "5Y",
      "All",
    ]);
    expect(buttons.map((b) => b.getAttribute("aria-pressed"))).toEqual([
      "true",
      "false",
      "false",
      "false",
      "false",
      "false",
      "false",
    ]);
    // Native buttons, none disabled: Tab reaches every option.
    expect(buttons.every((b) => b.tagName === "BUTTON" && b.getAttribute("disabled") === null)).toBe(
      true,
    );
  });

  it("choosing a range re-fetches with that range's `from`", async () => {
    renderPage(<Investments />);
    const toggle = await screen.findByRole("button", { name: "Show MSFT price history" });
    fireEvent.click(toggle);
    const group = await screen.findByRole("group", { name: /MSFT price history range/ });

    const fiveYear = Array.from(group.querySelectorAll("button")).find(
      (b) => b.textContent === "5Y",
    );
    fireEvent.click(fiveYear!);

    await waitFor(() => expect(h.getPriceHistory).toHaveBeenCalledTimes(2));
    const second = h.getPriceHistory.mock.calls[1][0];
    expect(second.granularity).toBe("month"); // 5Y → month, docs/12's table
    // 60 months back from TODAY — derived from the clock, so it holds
    // whenever this runs, and a genuinely different window from 1M's default.
    expect(second.from).toBe(monthsBack(60));
    expect(second.from).not.toBe(monthsBack(1));
    expect(second.to).toBe(iso());
  });
});

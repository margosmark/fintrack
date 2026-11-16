// M141 — no-quote symbols must not hang the UI in "yet"-forever (docs/09 §No public quote).
//
// The reported defect: adding "APOTEX" (private since 2022, no listing) landed
// on a SecurityDetail page reading "No prices yet — nothing has been fetched
// for this symbol **yet**" over a big blue "Refresh prices" button that could
// never succeed. The fix has three UI halves, each pinned here:
//
//   S1 the add form: a symbol Yahoo disowns raises a NON-BLOCKING warning with
//      an explicit "Add anyway" — never a hard block (a never-fetched symbol is
//      legitimate to watch), and a network failure adds silently.
//   S2 the Detail page: the empty state states the real reason and DISABLES
//      "Refresh prices" with that reason (the M140 G2 pattern), keeping a
//      "Try refresh" as the one path that can still probe the row.
//   S3 the watchlist row says "no public quote", not "no price".
//
// Harness mirrors m129DayDelta.test.tsx: the real pages, the real query
// client, mocked domain APIs, a FakeEventSource.
import type { ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";
import { SecurityDetail } from "../src/pages/SecurityDetail";

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
  /** The Security row as the server now sends it (M141 adds the dead block). */
  const security = (over: Record<string, unknown> = {}) => ({
    id: 7,
    symbol: "APOTEX",
    name: "Apotex Inc.",
    kind: "stock",
    listing_currency: "USD",
    exchange: "",
    dead_symbol: false,
    quote_miss_count: 0,
    dead_symbol_reason: null,
    ...over,
  });

  /** The overview payload — `latest: null` is the "no prices" state. */
  const overview = (sec: Record<string, unknown>) => ({
    security: security(sec),
    latest: null,
    stats: { high_52w: null, low_52w: null, change_pct: null, cached_days: 0 },
    history: [],
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
    watchlisted: true,
    fundamentals: null,
    holdings: null,
    recent_trades: [],
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
    checkQuote: vi.fn(),
    createSecurity: vi.fn(),
    refreshSecurity: vi.fn(),
    addWatchlistEntry: vi.fn(),
    removeWatchlistEntry: vi.fn(),
    user,
    brokerage,
    security,
    overview,
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
  return {
    ...actual,
    getWatchlist: h.getWatchlist,
    addWatchlistEntry: h.addWatchlistEntry,
    removeWatchlistEntry: h.removeWatchlistEntry,
  };
});
vi.mock("../src/api/securities", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/securities")>();
  return {
    ...actual,
    listSecurities: h.listSecurities,
    getSecurityOverview: h.getSecurityOverview,
    checkQuote: h.checkQuote,
    createSecurity: h.createSecurity,
    refreshSecurity: h.refreshSecurity,
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
vi.stubGlobal("EventSource", FakeEventSource);

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

/** The dead-symbol overview: no price, and Yahoo has disowned the ticker. */
const DEAD_OVERVIEW = {
  dead_symbol: true,
  quote_miss_count: 3,
  dead_symbol_reason: "$APOTEX: possibly delisted; no timezone found",
};

beforeEach(() => {
  h.restoreSession.mockResolvedValue({
    access_token: "token",
    token_type: "bearer",
    user: h.user,
  });
  h.me.mockResolvedValue(h.user);
  h.listAccounts.mockResolvedValue({ items: [h.brokerage], total: 1, limit: 200, offset: 0 });
  h.getPositions.mockResolvedValue({
    items: [],
    total: 0,
    total_unrealized_cad: null,
    total_value_cad: null,
    cash_cad: null,
    portfolio_value_cad: null,
    total_day_change_cad: null,
    total_day_change_pct: null,
    day_change_as_of: null,
    day_change_mixed_baseline: false,
  });
  h.getPortfolioValueHistory.mockResolvedValue({
    items: [],
    account_ids: [],
    stats: { change: null, change_pct: null, start_value: null, end_value: null },
    markers: [],
  });
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.listTrades.mockResolvedValue({ items: [], total: 0 });
  h.getDividendSuggestions.mockResolvedValue({ suggestions: [] });
  h.getExposure.mockResolvedValue({ items: [], total: 0 });
  h.getBreakdown.mockResolvedValue({ items: [], total: 0, dimension: "sector" });
  h.getPriceHistory.mockResolvedValue({ items: [], count: 0, granularity: "day" });
  h.checkQuote.mockResolvedValue({ symbol: "AAPL", quote_found: true, reason: null });
  h.createSecurity.mockResolvedValue(h.security());
  h.refreshSecurity.mockResolvedValue(h.overview({}));
  h.addWatchlistEntry.mockResolvedValue({});
  h.removeWatchlistEntry.mockResolvedValue(undefined);
  FakeEventSource.instances = [];
});

afterEach(() => {
  vi.clearAllMocks();
});

// --- S2: the Detail page's honest state --------------------------------------

describe("SecurityDetail — a dead symbol", () => {
  it("states the reason instead of the forever-'yet' promise", async () => {
    h.getSecurityOverview.mockResolvedValue(h.overview(DEAD_OVERVIEW));
    renderSecurity();

    // BOTH cards state the verdict: the price card (S2) as soon as the
    // overview lands, the chart card only once its own price-history query
    // settles out of `isPending`. So the same title and the same Yahoo verdict
    // legitimately render TWICE, milliseconds apart. A strict `getByText` on
    // either copy is therefore a race in both directions: it fails with
    // "Found multiple elements" when the chart lands first and passes with one
    // match when it lands late — a load-dependent red at any date (observed
    // 1/3 full-suite faketime runs, 0/15 single-file runs, 0/10 real-clock).
    // Wait for the LATER, unique copy first, then pin both cards explicitly.
    expect(await screen.findByText(/will not track unless a public listing appears/)).toBeTruthy();
    await screen.findByText(/no closes to chart/);
    // The verbatim Yahoo verdict, in each card's own honest consequence.
    expect(screen.getAllByText(/No public quote for APOTEX/)).toHaveLength(2);
    expect(screen.getAllByText(/possibly delisted/)).toHaveLength(2);
    // The old copy — a permanently-false promise — is gone.
    expect(screen.queryByText(/nothing has been fetched for this symbol yet/)).toBeNull();
  });

  it("disables 'Refresh prices' WITH the reason (M140 G2 pattern)", async () => {
    h.getSecurityOverview.mockResolvedValue(h.overview(DEAD_OVERVIEW));
    renderSecurity();

    const button = await screen.findByText("Refresh prices");
    const control = button.closest("button");
    expect(control).toBeTruthy();
    expect((control as HTMLButtonElement).disabled).toBe(true);
    // The reason is rendered AND bound via aria-describedby.
    const reason = document.querySelector('[data-testid="empty-state-action-reason"]');
    expect(reason?.textContent).toMatch(/cannot produce a price/);
    expect(control?.getAttribute("aria-describedby")).toBe(reason?.id);
  });

  it("keeps a 'Try refresh' as the one path that can still probe the row", async () => {
    h.getSecurityOverview.mockResolvedValue(h.overview(DEAD_OVERVIEW));
    renderSecurity();

    const tryRefresh = await screen.findByTestId("try-refresh");
    expect((tryRefresh as HTMLButtonElement).disabled).toBe(false);
  });

  it("keeps the ordinary 'No prices yet' state for a symbol that is merely unfetched", async () => {
    h.getSecurityOverview.mockResolvedValue(h.overview({}));
    renderSecurity();

    expect(await screen.findByText("No prices yet")).toBeTruthy();
    const control = screen.getByText("Refresh prices").closest("button");
    expect((control as HTMLButtonElement).disabled).toBe(false);
  });
});

// --- S1: the add form's non-blocking warning ---------------------------------

/** Type into a field and fire the input event React listens for. */
function typeInto(element: HTMLElement, value: string): void {
  fireEvent.change(element, { target: { value } });
}

/** Open the New-security form (it sits behind the trade form's toggle) and
 *  fill the two `canCreate` fields. The Name input has no placeholder but
 *  sits inside its `<label>`. */
async function fillForm(symbol: string, name = "Example Inc."): Promise<void> {
  fireEvent.click(await screen.findByTestId("new-security-toggle"));
  typeInto(await screen.findByPlaceholderText("XEQT"), symbol);
  typeInto(screen.getByLabelText("Name"), name);
}

function submit(): void {
  fireEvent.click(screen.getByRole("button", { name: /Create security/ }));
}

describe("SecurityForm — the add-time quote check", () => {
  it("warns without blocking, and 'Add anyway' completes the add", async () => {
    h.checkQuote.mockResolvedValue({
      symbol: "APOTEX",
      quote_found: false,
      reason: "$APOTEX: possibly delisted; no timezone found",
    });
    renderInvestments();
    await fillForm("APOTEX");
    submit();

    // The warning appears and the security is NOT created yet.
    const warning = await screen.findByTestId("quote-warning");
    expect(warning.textContent).toMatch(/No public quote found for/);
    expect(warning.textContent).toMatch(/Add anyway\?/);
    expect(h.createSecurity).not.toHaveBeenCalled();

    // "Add anyway" is the explicit second click (the mutation is async).
    fireEvent.click(screen.getByTestId("add-anyway"));
    await waitFor(() => expect(h.createSecurity).toHaveBeenCalledTimes(1));
  });

  it("does not ask twice for the same symbol once confirmed", async () => {
    h.checkQuote.mockResolvedValue({ symbol: "APOTEX", quote_found: false, reason: "nope" });
    // Keep the create FAILING so the form (and the confirmation) survives —
    // a success resets the form and there would be nothing left to re-submit.
    h.createSecurity.mockRejectedValue(new Error("duplicate"));
    renderInvestments();
    await fillForm("APOTEX");
    submit();
    fireEvent.click(await screen.findByTestId("add-anyway"));
    await waitFor(() => expect(h.createSecurity).toHaveBeenCalledTimes(1));

    // Change nothing, submit again: the probe is skipped for a confirmed symbol.
    submit();
    await waitFor(() => expect(h.createSecurity).toHaveBeenCalledTimes(2));
    expect(h.checkQuote).toHaveBeenCalledTimes(1);
  });

  it("adds in ONE click for a symbol that has a quote", async () => {
    h.checkQuote.mockResolvedValue({ symbol: "AAPL", quote_found: true, reason: null });
    h.createSecurity.mockResolvedValue(h.security());
    renderInvestments();
    await fillForm("AAPL");
    submit();

    await waitFor(() => expect(h.createSecurity).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("quote-warning")).toBeNull();
  });

  it("adds silently when the probe could not be run (never reports 'no quote' on a network failure)", async () => {
    // quote_found: null — "we could not ask" is not "there is no quote".
    h.checkQuote.mockResolvedValue({ symbol: "AAPL", quote_found: null, reason: "429" });
    h.createSecurity.mockResolvedValue(h.security());
    renderInvestments();
    await fillForm("AAPL");
    submit();

    await waitFor(() => expect(h.createSecurity).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("quote-warning")).toBeNull();
  });

  it("adds silently when the check endpoint itself fails", async () => {
    h.checkQuote.mockRejectedValue(new Error("network down"));
    h.createSecurity.mockResolvedValue(h.security());
    renderInvestments();
    await fillForm("AAPL");
    submit();

    await waitFor(() => expect(h.createSecurity).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("quote-warning")).toBeNull();
  });

  it("cancel dismisses the warning without adding (the warnings' cancel path)", async () => {
    h.checkQuote.mockResolvedValue({ symbol: "APOTEX", quote_found: false, reason: "nope" });
    renderInvestments();
    await fillForm("APOTEX");
    submit();
    fireEvent.click(await screen.findByTestId("dismiss-quote-warning"));

    expect(screen.queryByTestId("quote-warning")).toBeNull();
    expect(h.createSecurity).not.toHaveBeenCalled();
  });
});

// --- S3: the watchlist row ---------------------------------------------------

describe("Watchlist row — 'no public quote' not 'no price'", () => {
  it("says the symbol has no public quote", async () => {
    h.getWatchlist.mockResolvedValue({
      items: [
        {
          security_id: 7,
          symbol: "APOTEX",
          name: "Apotex Inc.",
          listing_currency: "USD",
          latest_price: null,
          price_source: null,
          price_age_days: null,
          price_stale: null,
          dead_symbol: true,
          dead_symbol_reason: "$APOTEX: possibly delisted; no timezone found",
        },
      ],
      total: 1,
    });
    renderInvestments();

    expect(await screen.findByText("no public quote")).toBeTruthy();
    expect(screen.queryByText("no price")).toBeNull();
  });

  it("keeps 'no price' for a symbol that is merely unfetched", async () => {
    h.getWatchlist.mockResolvedValue({
      items: [
        {
          security_id: 8,
          symbol: "NEWCO",
          name: "New Co.",
          listing_currency: "CAD",
          latest_price: null,
          price_source: null,
          price_age_days: null,
          price_stale: null,
          dead_symbol: false,
          dead_symbol_reason: null,
        },
      ],
      total: 1,
    });
    renderInvestments();

    expect(await screen.findByText("no price")).toBeTruthy();
    expect(screen.queryByText("no public quote")).toBeNull();
  });
});

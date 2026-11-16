// F-12 (docs/46 §D2) — /securities/:id's Fundamentals card stops printing raw
// money while the display mask is on.
//
// The finding, live on a masked demo session: the stats card right above it
// read `52W HIGH ••••• | 52W LOW ••••• | 365D CHANGE +23.67%`, while the
// Fundamentals card underneath printed `EPS 15.79 | P/E 17.67 | MARKET CAP
// 386.27B | DIV YIELD % 2.55 | BETA 0.906 | 52W RANGE 201.76 – 306.38` — plus
// the same strings again in six `title=` tooltips (8 text + 6 title hits, the
// whole-page raw-money count while masked).
//
// Cause: `fmtResearch()` — "compact display for research numbers" — never
// consulted `isMasked()`, and `FundamentalsCard` carried no `useMasked()`
// subscription. Raw interpolation OUTSIDE the tracked helpers is exactly the
// class `scripts/sweep_money.mjs` cannot see (the F-9/F-10/F-11 shape), which
// is why this needs a behavioural pin rather than a sweep row alone.
//
// Three tests, mirroring tests/f11ForecastMask.test.tsx:
//   1. MASKED scan — the real <SecurityDetail/> inside the real providers: no
//      `\d{1,3}(,\d{3})*\.\d{2}` anywhere in the text OR in any title/aria-label/
//      alt attribute (percentages excluded, §D2), with the card's six figures
//      and their tooltips bulleted, the stats strip unchanged, and the sole
//      surviving decimal on the whole page being `+23.67%`;
//   2. UNMASKED control — the same DOM prints the response's own figures and
//      the scanner FINDS them, so the masked zero is not a vacuous pass;
//   3. the flip — same mount, same nodes, both directions, zero refetch.
//
// House matcher style: `.getAttribute(...)`, no jest-dom.
import type { ReactElement } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";
import { ThemeProvider } from "../src/theme/ThemeProvider";
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

  /**
   * The demo security's own figures — the exact strings F-12 was filed on, so
   * the masked/unmasked pair is the finding's, not a synthetic stand-in.
   * `trailing_eps` carries the Decimal serialization noise `fmtResearch`
   * exists to strip, which pins that the UNMASKED formatting is unchanged.
   */
  const fundamentals = {
    trailing_eps: "15.79000000",
    pe_ratio: "17.67",
    market_cap: "386270000000.00",
    dividend_yield_pct: "2.55",
    beta: "0.906",
    sector: "Financial Services",
    industry: "Banks — Diversified",
    country: "Canada",
    fifty_two_week_high: "306.38",
    fifty_two_week_low: "201.76",
    source: "yfinance",
    fetched_at: "2026-10-15",
  };

  /** One holding and one trade, so the page's OTHER money surfaces are live
   *  beside the card under test — the sibling contradiction is the finding. */
  const position = {
    account_id: 1,
    account_name: "TFSA",
    security_id: 1,
    symbol: "RY.TO",
    listing_currency: "CAD",
    quantity: "362",
    price_date: "2026-10-15",
    price: "291.15",
    price_source: "yfinance",
    price_age_days: 0,
    price_stale: false,
    book_cost_cad: "98000.00",
    spark: ["280", "285", "291"],
    market_value_native: "105396.00",
    market_value_cad: "105396.00",
    fx_rate_to_cad: "1",
    previous_close_native: "285.60",
    previous_close_date: "2026-10-14",
    day_change_native: "5.55",
    day_change_pct: 1.94,
    day_change_cad: "2009.10",
    unrealized_gain_cad: "7396.00",
    unrealized_gain_native: "7396.00",
    unrealized_gain_pct: 7.55,
  };

  const overview = {
    security: {
      id: 1,
      symbol: "RY.TO",
      name: "Royal Bank of Canada",
      kind: "stock",
      listing_currency: "CAD",
      exchange: "TSX",
      dead_symbol: false,
      quote_miss_count: 0,
      dead_symbol_reason: null,
    },
    latest: {
      date: "2026-10-15",
      price: "291.15",
      source: "yfinance",
      age_days: 0,
      stale: false,
      previous_close: "285.60",
      day_change_native: "5.55",
      day_change_pct: 1.94,
    },
    // The stats strip masks these through fmtNative (the card that was RIGHT).
    stats: { high_52w: "306.38", low_52w: "201.76", change_pct: 23.67, cached_days: 52 },
    history: [
      { date: "2026-10-14", price: "285.60" },
      { date: "2026-10-15", price: "291.15" },
    ],
    positions: [position],
    totals: {
      quantity: "362",
      book_cost_cad: "98000.00",
      market_value_cad: "105396.00",
      unrealized_gain_cad: "7396.00",
      unrealized_gain_native: "7396.00",
      unrealized_gain_pct: 7.55,
      day_change_cad: "2009.10",
    },
    watchlisted: true,
    fundamentals,
    holdings: null,
    recent_trades: [
      {
        id: 9,
        account_id: 1,
        action: "buy",
        date: "2026-10-15",
        quantity: "100",
        price_native: "291.15",
      },
    ],
  };

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
    overview,
    fundamentals,
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
      const types = actual.INVESTMENT_ACCOUNT_TYPES as Record<string, boolean | undefined>;
      const investmentAccounts = (accounts.data?.items ?? []).filter(
        (a: { type: string }) => types[a.type] === true,
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

const renderSecurity = () => wrap(<SecurityDetail />, "/securities/:securityId", "/securities/1");

/**
 * The security scanner's own rule (the acceptance's `\d{1,3}(,\d{3})*\.\d{2}`),
 * applied to every rendered TEXT RUN and to the `title` / `aria-label` / `alt`
 * attributes — the F-12 finding counted 6 of the latter, so a text-only scan
 * would have missed half the defect.
 *
 * A "run" is CONSECUTIVE SIBLING TEXT NODES merged: JSX writes the stats
 * strip's change as three children (`"+"`, `"23.67"`, `"%"`), and the eye —
 * and any `innerText`-based scanner — reads one string. Splitting them would
 * report the legal `+23.67%` as `23.67` and make the acceptance impossible to
 * state. Each hit is returned WITH the run it came from, so a test can prove
 * the survivor really is a percentage (§D2) rather than a leaked amount.
 *
 * With `stripPercentages` the §D2-legal strings are removed first — that is
 * the acceptance's own "percentages excluded" reading, and it must come back
 * EMPTY on a masked page.
 */
function moneyRuns(root: HTMLElement, stripPercentages = false): Array<{ hit: string; run: string }> {
  const money = /\d{1,3}(?:,\d{3})*\.\d{2}/g;
  const stripPct = (s: string) => (stripPercentages ? s.replace(/\d+(?:\.\d+)?%/g, "") : s);
  const found: Array<{ hit: string; run: string }> = [];

  // Merge each parent's consecutive text-node children into one run.
  const collectRuns = (parent: Node): void => {
    let run = "";
    const flush = () => {
      if (run) for (const hit of stripPct(run).match(money) ?? []) found.push({ hit, run });
      run = "";
    };
    for (const child of Array.from(parent.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) run += child.textContent ?? "";
      else {
        flush();
        collectRuns(child);
      }
    }
    flush();
  };
  collectRuns(root);

  for (const el of root.querySelectorAll("*")) {
    for (const attr of ["title", "aria-label", "alt"]) {
      const value = el.getAttribute(attr);
      if (value) for (const hit of stripPct(value).match(money) ?? []) found.push({ hit, run: value });
    }
  }
  return found;
}

/** Bare matches, for count-shaped assertions. */
function rawMoneyHits(root: HTMLElement, stripPercentages = false): string[] {
  return moneyRuns(root, stripPercentages).map((m) => m.hit);
}

/** The `<dd>` that follows a given `<dt>` label, or null. */
function valueFor(label: string): HTMLElement | null {
  const dt = [...document.querySelectorAll("dt")].find((el) => el.textContent === label);
  return (dt?.nextElementSibling as HTMLElement | null) ?? null;
}

/** Every domain call the page could make — a flip must not move this. */
function apiCallCount(): number {
  return [
    h.getSecurityOverview,
    h.getPriceHistory,
    h.listAccounts,
    h.getPositions,
    h.getWatchlist,
    h.listTrades,
    h.getDividendSuggestions,
  ].reduce((sum, fn) => sum + fn.mock.calls.length, 0);
}

/** Raw `fetch` calls so far — the network the acceptance asks to be 0/flip. */
function fetchCount(): number {
  return (globalThis.fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length;
}

beforeEach(() => {
  sessionStorage.clear();
  // `act` because a tree from the previous test may still be mounted when the
  // store notifies its subscribers.
  act(() => initMaskForSession());
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  vi.spyOn(globalThis, "fetch");
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
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.listTrades.mockResolvedValue({ items: [], total: 0 });
  h.getDividendSuggestions.mockResolvedValue({ suggestions: [] });
  h.getExposure.mockResolvedValue({ items: [], total: 0 });
  h.getBreakdown.mockResolvedValue({ items: [], total: 0, dimension: "sector" });
  h.getPriceHistory.mockResolvedValue({ items: [], count: 0, granularity: "day" });
  h.getSecurityOverview.mockResolvedValue(h.overview);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  sessionStorage.clear();
  act(() => initMaskForSession());
  vi.clearAllMocks();
});

// --- 1. the masked scan ------------------------------------------------------

describe("F-12 — the Fundamentals card under a masked session", () => {
  it("prints no raw money anywhere on the page: text, title, aria-label or alt", async () => {
    setMasked(true);
    const { container } = renderSecurity();

    // The card must actually be rendering figures, or the scan is vacuous.
    expect(await screen.findByText("Fundamentals")).toBeTruthy();
    await screen.findByText("Market cap");
    await waitFor(() => expect(valueFor("EPS")?.textContent).toBe(MASK_TEXT));

    // The six research figures — value AND tooltip — plus the page as a whole.
    for (const label of ["EPS", "P/E", "Market cap", "Div yield %", "Beta"]) {
      const dd = valueFor(label);
      expect(dd, `no <dd> for ${label}`).toBeTruthy();
      expect(dd?.textContent).toBe(MASK_TEXT);
      expect(dd?.getAttribute("title")).toBe(MASK_TEXT);
    }
    // The range is a PAIR of prices, so it reads as a pair of bullets — in the
    // value and in the tooltip alike.
    const range = valueFor("52w range");
    expect(range?.textContent).toBe(`${MASK_TEXT} – ${MASK_TEXT}`);
    expect(range?.getAttribute("title")).toBe(`${MASK_TEXT} – ${MASK_TEXT}`);

    // Percentages excluded, per §D2: ZERO raw hits on the whole page.
    expect(rawMoneyHits(container, true)).toEqual([]);
    // Un-excluded, every decimal still on screen is inside a percentage run —
    // §D2 rules a proportional value visible, and none of them is an amount.
    const survivors = moneyRuns(container);
    expect(survivors.length).toBeGreaterThan(0);
    for (const { run } of survivors) expect(run).toContain("%");
    expect(container.textContent).toContain("+23.67%");

    // The sibling that was RIGHT all along, still right: the stats strip's
    // own figures, the holdings table, the trade's price — all bullets…
    expect(valueFor("52w high")?.textContent).toBe(MASK_TEXT);
    expect(valueFor("52w low")?.textContent).toBe(MASK_TEXT);
    expect(container.textContent).toContain(MASK_TEXT);

    // …while the §D2 percentages stay readable: a percentage is proportional,
    // so it leaks no scale.
    expect(container.textContent).toContain("+23.67%");
  });

  it("unmasked, the same card prints the response's figures — and the scanner finds them", async () => {
    initMaskForSession();
    const { container } = renderSecurity();

    await screen.findByText("Market cap");
    await waitFor(() => expect(valueFor("EPS")?.textContent).toBe("15.79"));

    // The unmasked string is the compact form of the response's own value:
    // Decimal serialization noise stripped ("15.79000000"), caps abbreviated.
    expect(valueFor("EPS")?.getAttribute("title")).toBe("15.79");
    expect(valueFor("P/E")?.textContent).toBe("17.67");
    expect(valueFor("Market cap")?.textContent).toBe("386.27B");
    expect(valueFor("Market cap")?.getAttribute("title")).toBe("386.27B");
    expect(valueFor("Div yield %")?.textContent).toBe("2.55");
    expect(valueFor("Beta")?.textContent).toBe("0.906");
    expect(valueFor("52w range")?.textContent).toBe("201.76 – 306.38");

    // Control half: the masked zero above must not be a scanner that finds
    // nothing either way. The card alone accounts for six `title=` hits and
    // seven numbers with §D2's percentage exclusion OFF — the finding's shape.
    const hits = rawMoneyHits(container);
    expect(hits.length).toBeGreaterThanOrEqual(8);
    expect(hits).toContain("386.27");
    expect(hits).toContain("201.76");
    expect(rawMoneyHits(container, true).length).toBeGreaterThanOrEqual(6);
    expect(container.textContent).not.toContain(MASK_TEXT);
  });

  it("flips both ways on the SAME nodes with zero refetch", async () => {
    initMaskForSession();
    const { container } = renderSecurity();

    await screen.findByText("Market cap");
    await waitFor(() => expect(valueFor("EPS")?.textContent).toBe("15.79"));

    // Pin node identity: a flip must RE-RENDER, never remount (a remount
    // would drop component state and, with react-query, re-fetch).
    const eps = valueFor("EPS");
    const range = valueFor("52w range");
    const marketCap = valueFor("Market cap");
    const callsBefore = apiCallCount();
    const fetchBefore = fetchCount();
    const overviewCalls = h.getSecurityOverview.mock.calls.length;
    expect(rawMoneyHits(container).length).toBeGreaterThanOrEqual(8);
    expect(rawMoneyHits(container, true).length).toBeGreaterThanOrEqual(6);

    // The chip's own store write (docs/46 §D3: one synchronous write, no I/O).
    act(() => setMasked(true));
    await waitFor(() => expect(rawMoneyHits(container, true)).toEqual([]));
    for (const { run } of moneyRuns(container)) expect(run).toContain("%");

    expect(valueFor("EPS")).toBe(eps);
    expect(valueFor("52w range")).toBe(range);
    expect(valueFor("Market cap")).toBe(marketCap);
    expect(eps?.textContent).toBe(MASK_TEXT);
    expect(eps?.getAttribute("title")).toBe(MASK_TEXT);
    expect(range?.textContent).toBe(`${MASK_TEXT} – ${MASK_TEXT}`);
    expect(range?.getAttribute("title")).toBe(`${MASK_TEXT} – ${MASK_TEXT}`);
    expect(marketCap?.textContent).toBe(MASK_TEXT);
    // The percentage §D2 keeps visible survives the flip in the other
    // direction — masking it would be a NEW violation of D2, not this fix.
    expect(container.textContent).toContain("+23.67%");

    expect(apiCallCount()).toBe(callsBefore);
    expect(fetchCount()).toBe(fetchBefore);

    act(() => setMasked(false));
    await waitFor(() => expect(valueFor("EPS")?.textContent).toBe("15.79"));
    expect(valueFor("52w range")?.textContent).toBe("201.76 – 306.38");
    expect(valueFor("Market cap")?.getAttribute("title")).toBe("386.27B");
    expect(rawMoneyHits(container).length).toBeGreaterThanOrEqual(8);
    expect(rawMoneyHits(container, true).length).toBeGreaterThanOrEqual(6);

    // Still zero: neither the domain mocks nor raw fetch moved in either
    // direction (the acceptance's "0 network requests per flip").
    expect(apiCallCount()).toBe(callsBefore);
    expect(fetchCount()).toBe(fetchBefore);
    expect(h.getSecurityOverview.mock.calls.length).toBe(overviewCalls);
  });
});

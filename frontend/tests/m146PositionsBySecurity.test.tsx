// M146 — Positions by security: the client-side fold, the lens that renders
// it, and the equality that makes the two views trustworthy.
//
//   A the FOLD (pure): one group per security_id (never per symbol), sums of
//     the SERVER's own per-row figures, VALUE-WEIGHTED percents, one spark,
//     nulls that stay null, and the market-value-desc order.
//   B the LENS (DOM): the toggle, the group roll-up with its "N accounts"
//     chip, the inline per-account sub-rows (linked accounts, one panel open
//     at a time), the flat single-account row with NO chevron, the all-null
//     "no price" group, and — the trust check between the two perspectives —
//     a Σ row that is BYTE-IDENTICAL across both views.
//
// The fixtures mirror the demo acceptance shape: VEQT across 5 accounts,
// VXUS across 2, ZGRO in 1, plus a security nobody has priced.

import type { ReactElement } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";
import { groupPositionsBySecurity } from "../src/lib/positionGroups";
import type { Position, PositionsResponse } from "../src/api/portfolio";

// The lens key is tenant-scoped (F-6): AuthProvider below resolves
// `user-1`, so `tenantScope` appends that id to the base key.
const VIEW_KEY = "fintrack:positions_view.user-1";

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

  /** One positions row as the SERVER sends it (docs/08, M129/M130 block). */
  const position = (over: Record<string, unknown> = {}) => ({
    account_id: 1,
    account_name: "TFSA",
    security_id: 7,
    symbol: "MSFT",
    listing_currency: "CAD",
    quantity: "10",
    price_date: "2026-10-08",
    price: "100.00",
    price_source: "yfinance",
    price_age_days: 1,
    price_stale: false,
    book_cost_cad: "900.00",
    spark: ["95", "97", "100"],
    market_value_native: "1000.00",
    market_value_cad: "1000.00",
    fx_rate_to_cad: "1",
    previous_close_native: "99.00",
    previous_close_date: "2026-10-07",
    day_change_native: "1.00",
    day_change_pct: 1.01,
    day_change_cad: "10.00",
    unrealized_gain_cad: "100.00",
    unrealized_gain_native: "100.00",
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
    getSecurityOverview: vi.fn(),
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
  return { ...actual, listSecurities: h.listSecurities, getSecurityOverview: h.getSecurityOverview };
});
vi.mock("../src/api/trades", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/trades")>();
  return { ...actual, listTrades: h.listTrades, getDividendSuggestions: h.getDividendSuggestions };
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

function wrap(ui: ReactElement, entry = "/investments") {
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

// --- the acceptance fixture -------------------------------------------------

const ACCOUNTS = ["TFSA", "RRSP", "Non-registered", "RESP", "FHSA"];

/** VEQT in 5 accounts — the duplicated-holding case the fold exists for. */
const VEQT_ROWS = ACCOUNTS.map((accountName, index) =>
  h.position({
    account_id: index + 1,
    account_name: accountName,
    security_id: 101,
    symbol: "VEQT",
    quantity: String(100 * (index + 1)),
    price: "40.00",
    price_date: "2026-10-08",
    book_cost_cad: `${3000 * (index + 1)}.00`,
    market_value_native: `${4000 * (index + 1)}.00`,
    market_value_cad: `${4000 * (index + 1)}.00`,
    previous_close_native: "39.60",
    day_change_native: "0.40",
    day_change_pct: 1.01,
    day_change_cad: `${40 * (index + 1)}.00`,
    unrealized_gain_cad: `${1000 * (index + 1)}.00`,
    unrealized_gain_native: `${1000 * (index + 1)}.00`,
    unrealized_gain_pct: 33.33,
  }),
);

/** VXUS in 2 accounts, one of them with NO cached price at all. */
const VXUS_ROWS = [
  h.position({
    account_id: 1,
    account_name: "TFSA",
    security_id: 102,
    symbol: "VXUS",
    quantity: "50",
    price: "60.00",
    book_cost_cad: "2000.00",
    market_value_native: "3000.00",
    market_value_cad: "3000.00",
    previous_close_native: null,
    day_change_native: null,
    day_change_pct: null,
    day_change_cad: null,
    unrealized_gain_cad: "1000.00",
    unrealized_gain_native: "1000.00",
    unrealized_gain_pct: 50,
  }),
  h.position({
    account_id: 2,
    account_name: "RRSP",
    security_id: 102,
    symbol: "VXUS",
    quantity: "100",
    price: "60.00",
    book_cost_cad: "4000.00",
    market_value_native: "6000.00",
    market_value_cad: "6000.00",
    previous_close_native: null,
    day_change_native: null,
    day_change_pct: null,
    day_change_cad: null,
    unrealized_gain_cad: "2000.00",
    unrealized_gain_native: "2000.00",
    unrealized_gain_pct: 50,
  }),
];

/** ZGRO in ONE account — renders flat, no chevron. */
const ZGRO_ROW = h.position({
  account_id: 1,
  account_name: "TFSA",
  security_id: 103,
  symbol: "ZGRO",
  quantity: "10",
  price: "250.00",
  book_cost_cad: "2000.00",
  market_value_native: "2500.00",
  market_value_cad: "2500.00",
  previous_close_native: "249.00",
  day_change_native: "1.00",
  day_change_pct: 0.4,
  day_change_cad: "10.00",
  unrealized_gain_cad: "500.00",
  unrealized_gain_native: "500.00",
  unrealized_gain_pct: 25,
});

/** A security in 2 accounts that nobody has priced — the "no price" group. */
const NOPRICE_ROWS = [3, 4].map((accountId, index) =>
  h.position({
    account_id: accountId,
    account_name: ACCOUNTS[accountId - 1],
    security_id: 104,
    symbol: "NOQUOTE",
    quantity: String(5 * (index + 1)),
    price: null,
    price_date: null,
    price_source: null,
    price_age_days: null,
    price_stale: null,
    book_cost_cad: null,
    spark: [],
    market_value_native: null,
    market_value_cad: null,
    fx_rate_to_cad: null,
    previous_close_native: null,
    previous_close_date: null,
    day_change_native: null,
    day_change_pct: null,
    day_change_cad: null,
    unrealized_gain_cad: null,
    unrealized_gain_native: null,
    unrealized_gain_pct: null,
  }),
);

const ALL_ROWS: Position[] = [
  ...(VEQT_ROWS as Position[]),
  ...(VXUS_ROWS as Position[]),
  ZGRO_ROW as unknown as Position,
  ...(NOPRICE_ROWS as Position[]),
];

/** The server's OWN totals (docs/08) — the Σ row renders these, both views. */
const PAYLOAD: PositionsResponse = {
  items: ALL_ROWS,
  total: ALL_ROWS.length,
  total_value_cad: "71500.00",
  total_unrealized_cad: "18500.00",
  cash_cad: "0.00",
  portfolio_value_cad: "71500.00",
  total_day_change_cad: "610.00",
  total_day_change_pct: 0.86,
  day_change_as_of: "2026-10-07",
  day_change_mixed_baseline: false,
} as PositionsResponse;

beforeEach(() => {
  FakeEventSource.instances = [];
  localStorage.removeItem(VIEW_KEY);
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
  h.getPortfolioValueHistory.mockResolvedValue({
    account_ids: [1],
    from: "2026-04-10",
    to: "2026-10-10",
    granularity: "day",
    requested_granularity: "day",
    currency: "CAD",
    native_currency: "CAD",
    min_event_value: "0",
    has_data: false,
    buckets: [],
    events: [],
    stats: {
      from_bucket: null,
      to_bucket: null,
      from_value_cad: null,
      to_value_cad: null,
      change_cad: null,
      change_pct: null,
    },
  });
  h.getPositions.mockResolvedValue(PAYLOAD);
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  localStorage.removeItem(VIEW_KEY);
});

// --- A: the fold ------------------------------------------------------------

describe("A — the by-security fold (lib/positionGroups)", () => {
  it("groups by security_id, never by the symbol string", () => {
    // M143's bare-ticker case: TWO securities can read the same ticker on
    // different exchanges. Folding on the string would merge them into one
    // row; folding on the id keeps them apart.
    const rows = [
      h.position({ security_id: 1, symbol: "VEQT", quantity: "1", market_value_cad: "40.00" }),
      h.position({ security_id: 2, symbol: "VEQT", quantity: "1", market_value_cad: "41.00" }),
    ] as unknown as Position[];
    const groups = groupPositionsBySecurity(rows);
    expect(groups).toHaveLength(2);
    expect(groups.map((g) => g.security_id)).toEqual([2, 1]); // value desc
    expect(groups.every((g) => g.account_count === 1)).toBe(true);
  });

  it("sums the SERVER's per-row figures for a 5-account holding", () => {
    const [veqt] = groupPositionsBySecurity(ALL_ROWS);
    expect(veqt.symbol).toBe("VEQT");
    expect(veqt.account_count).toBe(5);
    expect(veqt.quantity).toBe("1500");
    expect(veqt.market_value_cad).toBe("60000.00");
    expect(veqt.book_cost_cad).toBe("45000.00");
    expect(veqt.day_change_cad).toBe("600.00");
    expect(veqt.unrealized_gain_cad).toBe("15000.00");
  });

  it("weights percents by VALUE — never the mean of the rows' percents", () => {
    // A $2,000 holding up 0.50% and a $100 holding up 5.00%: the value-
    // weighted answer is ~0.72%, the naive mean 2.75%. The big holding must
    // pull the group toward itself. (A live payload gives one row per
    // account, so two DIFFERENT prices for one security are not reachable
    // there — this half pins the FORMULA; the gain % half below is exactly
    // the live shape, same price and different entry costs per account.)
    const rows = [
      h.position({
        security_id: 9,
        account_id: 1,
        quantity: "20",
        price: "100",
        market_value_cad: "2000.00",
        book_cost_cad: "1900.00",
        previous_close_native: "99.50",
        day_change_cad: "10.00",
        day_change_pct: 0.5,
        unrealized_gain_cad: "100.00",
        unrealized_gain_pct: 5.26,
      }),
      h.position({
        security_id: 9,
        account_id: 2,
        quantity: "1",
        price: "100",
        market_value_cad: "100.00",
        book_cost_cad: "50.00",
        previous_close_native: "95.00",
        day_change_cad: "5.00",
        day_change_pct: 5,
        unrealized_gain_cad: "50.00",
        unrealized_gain_pct: 100,
      }),
    ] as unknown as Position[];

    const [group] = groupPositionsBySecurity(rows);
    // Σ day Δ ÷ Σ (value − day Δ) = 15 ÷ 2085 = 0.7194% → 0.72%.
    expect(group.day_change_pct).toBe(0.72);
    // Σ gain ÷ Σ book = 150 ÷ 1950 = 7.6923% → 7.69%.
    expect(group.unrealized_gain_pct).toBe(7.69);
    // Guard: the fold is NOT the mean of the rows' percentages.
    expect(group.day_change_pct).not.toBeCloseTo((0.5 + 5) / 2, 2);
    expect(group.unrealized_gain_pct).not.toBeCloseTo((5.26 + 100) / 2, 2);
  });

  it("keeps nulls null: an all-unvalued group is never a zero", () => {
    const groups = groupPositionsBySecurity(ALL_ROWS);
    const noQuote = groups.find((group) => group.symbol === "NOQUOTE");
    expect(noQuote?.market_value_cad).toBeNull();
    expect(noQuote?.book_cost_cad).toBeNull();
    expect(noQuote?.day_change_cad).toBeNull();
    expect(noQuote?.day_change_pct).toBeNull();
    expect(noQuote?.unrealized_gain_cad).toBeNull();
    expect(noQuote?.unrealized_gain_pct).toBeNull();
    // …and an unvalued group sorts LAST, never first as a zero.
    expect(groups.at(-1)?.symbol).toBe("NOQUOTE");
  });

  it("sums only the rows whose figure is known (VXUS has no day Δ)", () => {
    const vxus = groupPositionsBySecurity(ALL_ROWS).find((group) => group.symbol === "VXUS");
    // Market value and gain still sum (both rows are valued)…
    expect(vxus?.market_value_cad).toBe("9000.00");
    expect(vxus?.unrealized_gain_cad).toBe("3000.00");
    expect(vxus?.unrealized_gain_pct).toBe(50); // 3000 ÷ 6000
    // …while the day % has no baseline at all, so it stays null.
    expect(vxus?.day_change_cad).toBeNull();
    expect(vxus?.day_change_pct).toBeNull();
  });

  it("draws ONE sparkline per group and orders by Σ value descending", () => {
    const groups = groupPositionsBySecurity(ALL_ROWS);
    expect(groups.map((group) => group.symbol)).toEqual(["VEQT", "VXUS", "ZGRO", "NOQUOTE"]);
    expect(groups[0].spark).toEqual(["95", "97", "100"]); // the security's closes, once
  });
});

// --- B: the lens ------------------------------------------------------------

const renderInvestments = () => wrap(<Investments />);

/** The table is ready once the first symbol cell has painted (flat view
 *  renders one row PER ACCOUNT, so "VEQT" alone is not a unique text). */
async function whenTableReady() {
  await screen.findAllByText("VEQT");
}

async function switchToSecurityView() {
  await whenTableReady();
  fireEvent.click(screen.getByRole("tab", { name: "By security" }));
}

describe("B — the Positions view toggle (M146)", () => {
  it("offers the lens on the portfolio view and persists it locally", async () => {
        renderInvestments();
    await screen.findByText("By account");
    await whenTableReady();
    expect(screen.getByRole("tab", { name: "By account" }).getAttribute("aria-selected")).toBe(
      "true",
    );

    await switchToSecurityView();

    // A lens on ONE table: localStorage only, no server write.
    expect(localStorage.getItem(VIEW_KEY)).toBe("security");
    expect(screen.getByRole("tab", { name: "By security" }).getAttribute("aria-selected")).toBe(
      "true",
    );
  });

  it("offers NO lens on an account-scoped page (every group would be one row)", async () => {
    wrap(<Investments accountId={1} />);
    await whenTableReady();
    expect(screen.queryByRole("tab", { name: "By security" })).toBeNull();
    // The account column is gone and the rows stay flat.
    expect(document.querySelectorAll('[data-testid="position-group-row"]').length).toBe(0);
  });

  it("collapses a 5-account holding into one group with the right sums", async () => {
        renderInvestments();
    // Flat: nine rows, one per account+security pair.
    await whenTableReady();
    expect(document.querySelectorAll('[data-testid="position-group-row"]').length).toBe(0);

    await switchToSecurityView();

    const groups = document.querySelectorAll('[data-testid="position-group-row"]');
    // VEQT ×5 accounts, VXUS ×2, NOQUOTE ×2 fold; ZGRO (one account) stays flat.
    expect(groups.length).toBe(3);
    const veqt = groups[0];
    expect(within(veqt as HTMLElement).getByText("5 accounts")).toBeTruthy();
    expect(veqt.textContent).toContain("1500"); // Σ quantity
    expect(veqt.textContent).toContain("$60,000.00"); // Σ market value (CAD)
    expect(veqt.textContent).toContain("$45,000.00"); // Σ book cost (display only)
    expect(veqt.textContent).toContain("+$15,000.00"); // Σ unrealized
    expect(veqt.textContent).toContain("+33.33%"); // Σ gain ÷ Σ book
    // The single-account security renders FLAT — no group row, no chip.
    expect(within(veqt as HTMLElement).queryByText("1 accounts")).toBeNull();
    const zgroRow = Array.from(document.querySelectorAll("tr")).find(
      (row) => row.textContent?.startsWith("TFSA") === true && row.textContent.includes("ZGRO"),
    );
    expect(zgroRow).toBeTruthy();
    expect(screen.queryByText("1 accounts")).toBeNull();
  });

  it("expands inline per-account rows, linked to their accounts, one open at a time", async () => {
        renderInvestments();
    await switchToSecurityView();

    const chevron = screen.getByRole("button", { name: /Show VEQT per-account rows/ });
    expect(chevron.getAttribute("aria-expanded")).toBe("false");
    expect(chevron.getAttribute("aria-controls")).toBeNull(); // no dangling idref

    fireEvent.click(chevron);

    // Five sub-rows, each linking its account to the account page.
    const panel = document.querySelector(
      '[data-testid="position-group-accounts"]',
    ) as HTMLElement;
    expect(panel).toBeTruthy();
    expect(panel.hidden).toBe(false);
    expect(
      within(panel)
        .getAllByRole("link")
        .map((link) => link.getAttribute("href"))
        .filter((href) => href?.startsWith("/accounts/") === true),
    ).toEqual(["/accounts/1", "/accounts/2", "/accounts/3", "/accounts/4", "/accounts/5"]);
    expect(
      screen.getByRole("button", { name: /Hide VEQT per-account rows/ }).getAttribute(
        "aria-expanded",
      ),
    ).toBe("true");

    // A second disclosure CLOSES the first — one panel open at a time.
    fireEvent.click(screen.getByRole("button", { name: /Show VXUS per-account rows/ }));
    expect(
      (document.querySelector('[data-testid="position-group-accounts"]') as HTMLElement).hidden,
    ).toBe(true);
    expect(screen.getByRole("button", { name: /Show VEQT per-account rows/ })).toBeTruthy();
  });

  it("gives a single-account security NO chevron — nothing to reveal", async () => {
        renderInvestments();
    await switchToSecurityView();

    expect(screen.getByRole("button", { name: /Show VEQT per-account rows/ })).toBeTruthy();
    // ZGRO holds one account, so its row is an ordinary position row: the
    // price-history chevron it already had is the ONLY button on it.
    expect(screen.queryByRole("button", { name: /ZGRO per-account rows/ })).toBeNull();
    expect(screen.queryByText("1 accounts")).toBeNull();
  });

  it("renders an all-unvalued group as \"no price\", never a fabricated $0", async () => {
        renderInvestments();
    await switchToSecurityView();

    const noQuote = Array.from(
      document.querySelectorAll('[data-testid="position-group-row"]'),
    ).find((row) => row.textContent?.includes("NOQUOTE")) as HTMLElement;
    expect(noQuote).toBeTruthy();
    expect(noQuote.textContent).toContain("no price — enter one manually");
    expect(within(noQuote).getByTestId("position-group-value-cad").textContent).toBe("—");
    expect(noQuote.textContent).not.toContain("$0.00");
  });

  it("shows a Σ row that is BYTE-IDENTICAL across both views", async () => {
        renderInvestments();

    const totalRow = () => screen.getByTestId("positions-total-row").textContent ?? "";
    await whenTableReady();
    const flat = totalRow();
    expect(flat).toContain("$71,500.00"); // the payload's own total_value_cad
    expect(flat).toContain("+$18,500.00"); // …and total_unrealized_cad

    await switchToSecurityView();
    const grouped = totalRow();
    expect(grouped).toBe(flat);
    // And back again — the equality is structural (one <tfoot>, the payload's
    // totals), not a coincidence of one direction.
    fireEvent.click(screen.getByRole("tab", { name: "By account" }));
    expect(totalRow()).toBe(flat);
  });
});

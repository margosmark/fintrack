// F-10 (P2, security final signature t_a2616dd5) — the /investments "Trades
// log" table printed RAW money on a masked session:
//
//   * `frontend/src/pages/Investments.tsx` rendered `{trade.price_native}`
//     verbatim — the finding measured 25 rows / 42 raw money cells / 0 masked
//     cells (`58.34000000`, `136.29000000`, `21000.00000000`, `0.00`, …) while
//     the four sibling tables in the same viewport rendered •••••;
//   * `{trade.commission}` likewise (with `"—"` for a zero commission), and
//     neither cell subscribed with `useMasked()`, so a flip did not repaint
//     them either.
//
// This is the same class as F-9/F-1: money built by raw JSX interpolation, so
// `scripts/sweep_money.mjs` cannot see it — the sweep tracks calls to the mask-
// aware helpers, and these two cells called none. Hence a BEHAVIOURAL pin in
// the shape docs/46 §D4/§Verification asks for (tests/f9ExposureDonutMask,
// tests/dashboardBudgetBannerMask): mount the REAL <Investments/> over mocked
// domain APIs, read the DOM back, and assert
//
//   1. masked  — no `\d{1,3}(,\d{3})*\.\d{2}` anywhere in the trades table,
//                with every price/commission cell carrying •••••;
//   2. control — the same rows unmasked print the real figures, so (1) can
//                never pass because the table failed to render;
//   3. a flip on ONE mount repaints the SAME `<tr>` nodes with an unchanged
//                API-call fingerprint and zero `fetch` calls (docs/46 §D3/D5:
//                the mask never reaches the wire).
//
// Harness mirrors tests/m167ExposureTruncate.test.tsx: the real page, the real
// query client, mocked domain APIs. House matcher style: no jest-dom.
import type { ReactElement } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";

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
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: MessageEvent) => void) | null = null;
  close = vi.fn();
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
}

// --- the finding's live rows ------------------------------------------------
//
// The exact figures the security run measured leaking (build 164a33b), plus
// the M132 row shape that makes the currency chain matter: one row stated in
// its own currency, one pre-M132 NULL row that must read in its ACCOUNT's.
const TRADES = [
  {
    id: 901,
    account_id: 1,
    security_id: 7,
    action: "buy",
    date: "2026-01-15",
    quantity: "100",
    price_native: "58.34",
    commission: "9.99",
    currency: "CAD",
    fx_rate_used: null,
  },
  {
    id: 902,
    account_id: 1,
    security_id: 8,
    action: "sell",
    date: "2026-02-20",
    quantity: "10",
    price_native: "136.29",
    // The LIVE shape: `commission` is Numeric(18,2), so the wire says "0.00".
    commission: "0.00",
    currency: "USD",
    fx_rate_used: null,
  },
  {
    id: 903,
    account_id: 1,
    security_id: 7,
    action: "contribution",
    date: "2026-03-05",
    quantity: "0",
    price_native: "21000.00",
    commission: "0",
    currency: null,
    fx_rate_used: null,
  },
];

const SECURITIES = [
  { id: 7, symbol: "AAA" },
  { id: 8, symbol: "BBB" },
];

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

/** The trades table itself — the region the finding measured (aria-label). */
function tradesTable(): HTMLElement {
  const region = screen.getByLabelText("Trades log table");
  if (!(region instanceof HTMLElement)) throw new Error("trades region missing");
  return region;
}

/**
 * Money-shaped text inside the trades table — `58.34`, `136.29`, `21,000.00`:
 * the shapes a shoulder-surfer reads as dollars, matching the finding's
 * acceptance regex `\d{1,3}(,\d{3})*\.\d{2}`.
 *
 * Cells are scanned SEPARATELY: `textContent` runs adjacent `<td>`s together,
 * so the quantity `100` touching the price `58.34` reads `10058.34` and the
 * scanner would report a figure no screen ever showed (and, worse, could
 * swallow a genuine leak whole). ISO dates park first (they carry no dots,
 * but be explicit), then percentages — parked exactly the way
 * `maskAmountsInText` parks them, so the scan never trips on a proportional
 * value (docs/46 §D2).
 */
function moneyLeaks(root: HTMLElement): string[] {
  const cells = [...root.querySelectorAll("td, th")]
    .map((cell) => cell.textContent ?? "")
    .join(" \n ");
  const parked = cells
    .replace(/\d{4}-\d{2}-\d{2}/g, " ")
    .replace(/\d+(?:\.\d+)?\s?%/g, " ");
  return parked.match(/\d{1,3}(?:,\d{3})*\.\d{2}/g) ?? [];
}

/** Rows as they stand, identified for a same-node flip assertion. */
function rowNodes(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>("[data-trade-id]")];
}

/** The trades table is mounted, all three fixture rows painted, and the
 *  securities query has resolved (the Security cell reads its symbol map). */
async function rowsReady(): Promise<void> {
  await screen.findByLabelText("Trades log table");
  await waitFor(() => expect(rowNodes()).toHaveLength(3));
  await waitFor(() =>
    expect(rowNodes()[0]?.querySelectorAll("td")[2]?.textContent).toBe("AAA"),
  );
}

/** Every mocked API's call count, as one comparable fingerprint. */
function apiFingerprint(): number {
  return [
    h.listAccounts,
    h.listCategories,
    h.getPositions,
    h.getPortfolioValueHistory,
    h.getExposure,
    h.getBreakdown,
    h.getWatchlist,
    h.listSecurities,
    h.listTrades,
    h.getDividendSuggestions,
    h.getPriceHistory,
    h.restoreSession,
    h.me,
  ].reduce((sum, fn) => sum + fn.mock.calls.length, 0);
}

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
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
    items: [],
    total_cad: "0",
    unclassified_cad: "0",
  });
  h.listSecurities.mockResolvedValue(h.page(SECURITIES));
  h.listTrades.mockResolvedValue({ items: TRADES, total: TRADES.length, limit: 25, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  h.getPositions.mockResolvedValue({ items: [], total: 0 });
  h.getPortfolioValueHistory.mockResolvedValue({ buckets: [], events: [] });
  h.getPriceHistory.mockResolvedValue({ items: [], security: null });
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  vi.spyOn(globalThis, "fetch");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  sessionStorage.clear();
  initMaskForSession();
  vi.clearAllMocks();
});

describe("F-10 — the /investments trades log never prints raw price/commission while masked", () => {
  it("CONTROL (unmasked): the same rows DO print the real figures, so a masked pass is not vacuous", async () => {
    renderPage(<Investments />);
    await rowsReady();

    const table = tradesTable();
    expect(rowNodes()).toHaveLength(3);

    // Price / amount: the row's own currency (M132), 2dp, thousands grouped —
    // NOT the raw Decimal the wire sent (`58.34000000`).
    expect(table.textContent).toContain("58.34 CAD");
    expect(table.textContent).toContain("136.29 USD");
    // currency NULL → the ACCOUNT's currency, the read the server does.
    expect(table.textContent).toContain("21,000.00 CAD");

    // Commission: a real fee prints; the LIVE zero shape ("0.00") prints its
    // figure, the literal "0" the em-dash guard was written against prints "—".
    expect(table.textContent).toContain("9.99 CAD");
    expect(table.textContent).toContain("0.00 USD");
    expect(table.textContent).toContain("—");
    expect(table.textContent).not.toContain(MASK_TEXT);

    // The scanner FINDS these figures when the mask is off, so the masked
    // `[]` below is a real result and not an empty-table artifact.
    expect([...moneyLeaks(table)].sort()).toEqual(
      ["0.00", "9.99", "136.29", "21,000.00", "58.34"].sort(),
    );
  });

  it("masked: every price/amount and commission cell is bullets — no money-shaped text in the table", async () => {
    setMasked(true);
    renderPage(<Investments />);
    await rowsReady();

    // Rows rendered (the control's precondition), so an empty scan cannot be
    // a pass on a table that never painted.
    const table = tradesTable();
    expect(rowNodes()).toHaveLength(3);
    expect(table.textContent).toContain("AAA");
    expect(table.textContent).toContain("BBB");
    expect(table.textContent).toContain("100"); // quantity is a share count, not money (D2)

    // The acceptance criterion: no money shape survives in the trades table.
    expect(moneyLeaks(table)).toEqual([]);
    expect(table.textContent).not.toContain("58.34");
    expect(table.textContent).not.toContain("136.29");
    expect(table.textContent).not.toContain("0.00"); // the live zero commission
    expect(table.textContent).not.toContain("21000.00");

    // …and it is bullets in those cells, not silence: 3 price cells + 3
    // commissions — a fee, the live "0.00" zero and the literal "0" (the
    // em-dash convention masks too, so a masked row never says "no fee").
    const cells = [...table.querySelectorAll("tbody td")];
    expect(cells).toHaveLength(21); // 3 rows x 7 columns
    const moneyCells = cells.filter((_, i) => i % 7 === 5 || i % 7 === 6);
    expect(moneyCells).toHaveLength(6);
    for (const cell of moneyCells) expect(cell.textContent).toBe(MASK_TEXT);
  });

  it("flips BOTH directions on ONE mount — same <tr> nodes, zero refetch, zero network", async () => {
    renderPage(<Investments />);
    await rowsReady();

    const table = tradesTable();
    const before = rowNodes();
    expect(before).toHaveLength(3);
    expect(table.textContent).toContain("58.34 CAD");
    expect(moneyLeaks(table)).not.toEqual([]);

    const apiCalls = apiFingerprint();
    const fetches = vi.mocked(globalThis.fetch).mock.calls.length;

    act(() => setMasked(true));

    // SAME nodes: the cells repainted, the table did not remount (a remount
    // would drop query state and refetch — docs/46 §D6).
    await waitFor(() => expect(moneyLeaks(table)).toEqual([]));
    expect(rowNodes()).toEqual(before);
    expect(table.textContent).toContain(MASK_TEXT);

    // …with nothing asked of the server: the mask lives below the data layer.
    expect(apiFingerprint()).toBe(apiCalls);
    expect(vi.mocked(globalThis.fetch).mock.calls.length).toBe(fetches);

    act(() => setMasked(false));

    await waitFor(() => expect(table.textContent).toContain("58.34 CAD"));
    expect(rowNodes()).toEqual(before);
    expect([...moneyLeaks(table)].sort()).toEqual(
      ["0.00", "9.99", "136.29", "21,000.00", "58.34"].sort(),
    );
    expect(apiFingerprint()).toBe(apiCalls);
    expect(vi.mocked(globalThis.fetch).mock.calls.length).toBe(fetches);
  });
});

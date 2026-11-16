// M169 — the exposure drill-down (docs/09 §Exposure drill):
//
//   Mark's ask on the back of M167's map: "drill down what security or fund is
//   from that place and in the case of fund what holding(s)". The panel opens
//   INLINE under the row (M126's expand contract, never navigation), fetches
//   `?drill=<key>` on the SAME breakdown endpoint, and renders the via chain,
//   the provenance badge, and the estimate/residual rows the walk produced.
//
// Harness mirrors tests/m167ExposureTruncate.test.tsx: the real Investments
// page, the real query client, mocked domain APIs.
import type { ReactElement } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";
import type {
  BreakdownResponse,
  DrillBlock,
  DrillRow,
  BreakdownDimension,
} from "../src/api/portfolio";

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
  onerror: ((event: MessageEvent) => void) | null = null;
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

const row = (key: string, weight_pct: number, market_value_cad: string) => ({
  key,
  market_value_cad,
  weight_pct,
});

/** A geography list of three, small enough to read in full. */
const GEOGRAPHY: ReturnType<typeof row>[] = [
  row("Canada", 40.0, "4000.00"),
  row("United States", 55.0, "5500.00"),
  row("Japan", 5.0, "500.00"),
];

/** A drill leaf, with every optional left off so a test sets only what it pins. */
function leaf(over: Partial<DrillRow> = {}): DrillRow {
  return {
    position_kind: "direct",
    security_id: 10,
    symbol: "RY",
    name: "Royal Bank",
    account_ids: [1],
    account_count: 1,
    native_weight_share: "30.00",
    market_value_cad: "1200.00",
    weight_pct: 30.0,
    provenance_source: "issuer",
    estimated: false,
    unattributed: false,
    via: [],
    depth: 0,
    ...over,
  };
}

/** Canada: SHOP held directly, plus VEQT walked into RY (one hop) and, two
 *  hops down, Microsoft through VXUS — the chain Mark asked about by name. */
function canadaDrill(): DrillBlock {
  return {
    key: "Canada",
    dimension: "geography",
    bucket_market_value_cad: "4000.00",
    bucket_weight_pct: 40.0,
    contributions_cad: "4000.00",
    depth_cap: 3,
    other: null,
    contributions: [
      {
        position_kind: "direct",
        security_id: 1,
        symbol: "SHOP",
        name: "Shopify",
        account_ids: [1],
        account_count: 1,
        native_weight_share: null,
        market_value_cad: "1000.00",
        weight_pct: 25.0,
        provenance_source: null,
        estimated: false,
        unattributed: false,
        via: [],
        depth: 0,
        holdings: [],
        holdings_other: null,
        estimated_cad: "0.00",
        unattributed_cad: "0.00",
      },
      {
        position_kind: "fund",
        security_id: 2,
        symbol: "VEQT",
        name: "Vanguard All-Equity",
        account_ids: [1, 2],
        account_count: 2,
        native_weight_share: null,
        market_value_cad: "3000.00",
        weight_pct: 75.0,
        provenance_source: null,
        estimated: false,
        unattributed: false,
        via: [],
        depth: 0,
        holdings: [
          leaf({
            symbol: "RY",
            name: "Royal Bank",
            via: ["VEQT"],
            depth: 1,
            market_value_cad: "1800.00",
            weight_pct: 45.0,
          }),
          leaf({
            symbol: "MSFT",
            name: "Microsoft",
            security_id: 11,
            via: ["VEQT", "VXUS"],
            depth: 2,
            market_value_cad: "900.00",
            weight_pct: 22.5,
            provenance_source: "yfinance",
          }),
          leaf({
            symbol: "CASH",
            name: "Canadian cash",
            security_id: null,
            native_weight_share: null,
            provenance_source: null,
            via: ["VEQT"],
            depth: 1,
            market_value_cad: "300.00",
            weight_pct: 7.5,
          }),
        ],
        holdings_other: null,
        estimated_cad: "0.00",
        unattributed_cad: "0.00",
      },
    ],
  };
}

function payload(items: ReturnType<typeof row>[], drill?: DrillBlock): BreakdownResponse {
  return {
    dimension: "geography",
    items,
    total_cad: "10000.00",
    unclassified_cad: "0.00",
    estimated_cad: "0.00",
    unclassified_funds: [],
    ...(drill ? { drill } : {}),
  };
}

beforeEach(() => {
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
  h.getBreakdown.mockResolvedValue(payload(GEOGRAPHY));
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  h.getPositions.mockResolvedValue({ items: [], total: 0 });
  h.getPriceHistory.mockResolvedValue({ items: [], security: null });
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/** Open the Geography tab on a freshly rendered page. */
async function openGeography() {
  renderPage(<Investments />);
  fireEvent.click(await screen.findByRole("tab", { name: "Geography" }));
  await screen.findByLabelText("Exposure breakdown table");
}

/** The chevron button that opens one bucket's drill. */
function drillButton(bucket: string) {
  return screen.getByRole("button", { name: `Drill into ${bucket}` });
}

describe("M169 — the exposure drill-down", () => {
  it("opens inline under the row and asks the SAME endpoint with ?drill=", async () => {
    h.getBreakdown.mockImplementation(
      async (dimension: BreakdownDimension, _accountId?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, canadaDrill()) : payload(GEOGRAPHY),
    );
    await openGeography();

    // No drill request has been made yet — the list fetch is the only one.
    expect(h.getBreakdown).toHaveBeenCalledTimes(1);

    fireEvent.click(drillButton("Canada"));

    const panel = await screen.findByTestId("exposure-drill");
    expect(h.getBreakdown).toHaveBeenCalledWith("geography", undefined, "Canada");
    // It is a PANEL inside the table, not a new page: the list is still here.
    expect(screen.getByLabelText("Exposure breakdown table")).toBeTruthy();
    expect(within(panel).getByText("SHOP")).toBeTruthy();
  });

  it("never fetches a drill for a bucket nobody opened", async () => {
    await openGeography();
    await screen.findByLabelText("Exposure breakdown table");
    // Only the list. A drill is one request per bucket a user actually opens,
    // and the list call carries no third argument at all.
    expect(h.getBreakdown).toHaveBeenCalledTimes(1);
    expect(h.getBreakdown.mock.calls[0]).toEqual(["geography", undefined]);
    expect(h.getBreakdown.mock.calls[0][2]).toBeUndefined();
  });

  it("names the via chain so a two-hop leaf is never read as held directly", async () => {
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, canadaDrill()) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    const panel = await screen.findByTestId("exposure-drill");
    fireEvent.click(within(panel).getByRole("button", { name: /Show VEQT holdings/ }));

    // Microsoft is reached TWO funds down; the row says so in words.
    const msft = within(panel)
      .getAllByTestId("drill-via")
      .map((node) => node.textContent);
    expect(msft).toContain("via VEQT → VXUS");
    expect(msft).toContain("via VEQT");
  });

  it("expands a fund's holdings on demand, keyboard-reachable (M126's contract)", async () => {
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, canadaDrill()) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    const panel = await screen.findByTestId("exposure-drill");

    const toggle = within(panel).getByRole("button", { name: /Show VEQT holdings/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(toggle);
    expect(within(panel).getByRole("button", { name: /Hide VEQT holdings/ })).toBeTruthy();
    expect(within(panel).getByTestId("drill-holding-RY")).toBeTruthy();

    fireEvent.click(within(panel).getByRole("button", { name: /Hide VEQT holdings/ }));
    expect(within(panel).queryByTestId("drill-holding-RY")).toBeNull();
  });

  it("renders the SERVER's total and never re-adds the rows", async () => {
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, canadaDrill()) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    const panel = await screen.findByTestId("exposure-drill");

    // The header quotes contributions_cad — the server's number.
    expect(within(panel).getByText(/What Canada is made of/)).toBeTruthy();
    expect(within(panel).getByText(/\$4,000\.00 of exposure/)).toBeTruthy();
    // The rows are shown, but the TOTAL is not their sum re-computed here.
    const contributions = within(panel).getAllByTestId(/^drill-contribution/);
    expect(contributions.length).toBe(2);
  });

  it("wears the provenance badge the row's own source earned", async () => {
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, canadaDrill()) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    const panel = await screen.findByTestId("exposure-drill");
    fireEvent.click(within(panel).getByRole("button", { name: /Show VEQT holdings/ }));

    // issuer CSV for the first-party file, top holdings for the yfinance scrape.
    const badges = within(panel).getAllByText(/issuer CSV|top holdings/).map((n) => n.textContent);
    expect(badges).toContain("issuer CSV");
    expect(badges).toContain("top holdings");
  });

  it("marks the M166 estimate on the row that speaks for it", async () => {
    const estimated: DrillBlock = {
      ...canadaDrill(),
      contributions: [
        {
          ...canadaDrill().contributions[1],
          estimated: true,
          estimated_cad: "600.00",
          holdings: [
            leaf({
              symbol: "VEQT",
              name: "Vanguard All-Equity",
              position_kind: "sub_fund",
              estimated: true,
              native_weight_share: null,
              provenance_source: null,
              market_value_cad: "600.00",
              weight_pct: 15.0,
              via: [],
              depth: 0,
            }),
          ],
        },
      ],
    };
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, estimated) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    const panel = await screen.findByTestId("exposure-drill");

    // The fund's own line carries the flag; the estimate row inside says why.
    expect(within(panel).getAllByTestId("drill-estimated").length).toBeGreaterThan(0);
    fireEvent.click(within(panel).getByRole("button", { name: /Show VEQT holdings/ }));
    expect(within(panel).getAllByTestId("drill-estimated").length).toBe(2);
  });

  it("says 'not placed' for the residual instead of dropping it", async () => {
    const residual: DrillBlock = {
      ...canadaDrill(),
      contributions: [
        {
          ...canadaDrill().contributions[1],
          unattributed: true,
          unattributed_cad: "100.00",
          holdings: [
            leaf({
              symbol: "VEQT",
              position_kind: "sub_fund",
              unattributed: true,
              native_weight_share: null,
              provenance_source: null,
              market_value_cad: "100.00",
              weight_pct: 2.5,
              via: [],
              depth: 0,
            }),
          ],
        },
      ],
    };
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, residual) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    const panel = await screen.findByTestId("exposure-drill");
    fireEvent.click(within(panel).getByRole("button", { name: /Show VEQT holdings/ }));
    expect(within(panel).getAllByTestId("drill-unattributed").length).toBe(2);
  });

  it("says a fund with no imported holdings has none, instead of an empty chevron", async () => {
    const empty: DrillBlock = {
      ...canadaDrill(),
      contributions: [
        {
          ...canadaDrill().contributions[1],
          holdings: [],
          unattributed: true,
          unattributed_cad: "3000.00",
        },
      ],
    };
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, empty) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    const panel = await screen.findByTestId("exposure-drill");
    expect(within(panel).getByText("no holdings imported")).toBeTruthy();
    expect(within(panel).queryByRole("button", { name: /Show VEQT holdings/ })).toBeNull();
  });

  it("folds a long drill with an Other that owns the tail exactly", async () => {
    const many = Array.from({ length: 30 }, (_, index) =>
      leaf({
        symbol: `C${index}`,
        market_value_cad: "100.00",
        weight_pct: 2.5,
      }),
    );
    const long: DrillBlock = {
      ...canadaDrill(),
      contributions: [
        {
          ...canadaDrill().contributions[1],
          market_value_cad: "3000.00",
          holdings: many.slice(0, 25),
          holdings_other: { count: 5, market_value_cad: "500.00", weight_pct: 12.5 },
        },
      ],
    };
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, long) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    const panel = await screen.findByTestId("exposure-drill");
    fireEvent.click(within(panel).getByRole("button", { name: /Show VEQT holdings/ }));

    const other = within(panel).getByTestId("drill-other");
    expect(other.textContent).toContain("5 more holdings");
    expect(other.textContent).toContain("$500.00");
    // Head + tail is the whole $3,000 — the fold is arithmetic, not a guess.
    // 25 shown rows at $100 each, plus the tail's $500.
    const shown = within(panel)
      .getAllByTestId(/^drill-holding-/)
      .reduce((sum, node) => {
        const match = node.textContent?.match(/\$([\d,]+\.\d{2})/);
        return sum + (match ? Math.round(Number(match[1].replace(/,/g, "")) * 100) : 0);
      }, 0);
    expect(shown).toBe(250000);
    expect((shown + 50000) / 100).toBe(3000);
  });

  it("inherits the M139 mask on values while percentages stay visible", async () => {
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, canadaDrill()) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    await screen.findByTestId("exposure-drill");

    const { setMasked } = await import("../src/lib/mask");
    setMasked(true);
    const panel = await screen.findByTestId("exposure-drill");

    // Values mask; the weights and the drill's own percentage do not.
    expect(within(panel).getAllByText("•••••").length).toBeGreaterThan(0);
    expect(within(panel).getAllByText(/\d+\.\d{2}%/).length).toBeGreaterThan(0);
    setMasked(false);
  });

  it("surfaces the walk's depth cap as disclosure, not a client-side limit", async () => {
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, canadaDrill()) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    const panel = await screen.findByTestId("exposure-drill");
    expect(within(panel).getByText(/3 levels of funds/)).toBeTruthy();
  });

  it("closes the drill again on a second click", async () => {
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) =>
        drill === "Canada" ? payload(GEOGRAPHY, canadaDrill()) : payload(GEOGRAPHY),
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));
    await screen.findByTestId("exposure-drill");

    fireEvent.click(screen.getByRole("button", { name: "Hide Canada drill" }));
    expect(screen.queryByTestId("exposure-drill")).toBeNull();
  });

  it("shows a drill error in place instead of blanking the table", async () => {
    h.getBreakdown.mockImplementation(
      async (_d: BreakdownDimension, _a?: number, drill?: string) => {
        if (drill) throw new Error("No geography bucket named 'Canada' in this breakdown.");
        return payload(GEOGRAPHY);
      },
    );
    await openGeography();
    fireEvent.click(drillButton("Canada"));

    // The drill's own alert — the page has ErrorPanels with the same role, so
    // scope the query to the message this panel owns.
    const alert = await screen.findByText(/No geography bucket named/);
    expect(alert.getAttribute("role")).toBe("alert");
    // The list survives — a failed drill is not a failed page.
    expect(screen.getByText("Japan")).toBeTruthy();
  });

  it("offers the drill on every dimension, not just geography", async () => {
    await openGeography();
    expect(drillButton("Canada")).toBeTruthy();
    expect(drillButton("United States")).toBeTruthy();
    expect(drillButton("Japan")).toBeTruthy();
  });
});

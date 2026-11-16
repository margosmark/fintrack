// M167 — the Exposure card's list fold (docs/09 §Exposure visuals):
//
//   a 40-country list is a chore to scan, so every dimension renders the SAME
//   list shape — the top 8 (top 10 for companies), an `Other` row that owns
//   the tail, and an expander that reveals the full list inline and remembers
//   itself for the session.
//
// Harness mirrors tests/m147UnclassifiedCopy.test.tsx: the real Investments
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
import type { BreakdownDimension, BreakdownRow } from "../src/api/portfolio";
import { formatCadCompact } from "../src/lib/format";
import { clearExpandedDimensions } from "../src/lib/exposureFold";

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

const row = (key: string, weight_pct: number, market_value_cad: string): BreakdownRow => ({
  key,
  market_value_cad,
  weight_pct,
});

/** The long tail Mark screenshots: eight countries you read, four you don't. */
const GEOGRAPHY_12: BreakdownRow[] = [
  row("United States", 44.7, "645646.42"),
  row("Canada", 20.49, "295961.03"),
  row("Japan", 7.53, "108762.3"),
  row("United Kingdom", 5.1, "73686.4"),
  row("Germany", 4.2, "60685.48"),
  row("France", 3.3, "47683.35"),
  row("Australia", 2.5, "36124.25"),
  row("Switzerland", 1.9, "27456.03"),
  row("India", 1.29, "18633.41"),
  row("Netherlands", 1.08, "15607.7"),
  row("Denmark", 0.9, "12999.9"),
  row("Spain", 0.71, "10261.47"),
];

const cents = (value: string) => Math.round(Number(value) * 100);
const totalOf = (rows: readonly BreakdownRow[]) =>
  (rows.reduce((sum, r) => sum + cents(r.market_value_cad), 0) / 100).toFixed(2);

function payload(dimension: BreakdownDimension, items: BreakdownRow[]) {
  return {
    dimension,
    items,
    total_cad: totalOf(items),
    unclassified_cad: "0.00",
  };
}

/** Click a dimension tab on the page that is already mounted. */
async function selectTab(tab: "Geography" | "Sector" | "Companies" | "Asset class") {
  fireEvent.click(await screen.findByRole("tab", { name: tab }));
  await screen.findByLabelText("Exposure breakdown table");
}

async function openTab(tab: "Geography" | "Sector" | "Companies" | "Asset class") {
  renderPage(<Investments />);
  await selectTab(tab);
}

function tableBody() {
  const region = screen.getByLabelText("Exposure breakdown table");
  return within(region as HTMLElement);
}

function bodyRowKeys(): string[] {
  return [...document.querySelectorAll<HTMLElement>("[data-exposure-row]")].map(
    (tr) => tr.dataset.exposureRow ?? "",
  );
}

beforeEach(() => {
  clearExpandedDimensions();
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
  h.getBreakdown.mockResolvedValue(payload("geography", GEOGRAPHY_12));
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  h.getPositions.mockResolvedValue({ items: [], total: 0 });
  h.getPriceHistory.mockResolvedValue({ items: [], security: null });
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  clearExpandedDimensions();
  window.localStorage.clear();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("M167 — top 8 + Other, then the full list inline", () => {
  it("collapses to the top 8 countries with an Other row that owns the tail", async () => {
    await openTab("Geography");

    expect(bodyRowKeys()).toEqual([
      "United States",
      "Canada",
      "Japan",
      "United Kingdom",
      "Germany",
      "France",
      "Australia",
      "Switzerland",
      "Other",
    ]);

    // Other is the tail's exact arithmetic, not a rounded guess.
    const tail = GEOGRAPHY_12.slice(8);
    const tailDollars = Number(
      (tail.reduce((sum, r) => sum + cents(r.market_value_cad), 0) / 100).toFixed(2),
    );
    const other = document.querySelector<HTMLElement>('[data-exposure-row="Other"]');
    expect(other?.textContent).toContain(`$${tailDollars.toLocaleString("en-CA")}`);
    expect(other?.textContent).toContain("3.98%");
  });

  it("names the fold in the expander and flips to a collapse control when open", async () => {
    await openTab("Geography");

    const expander = screen.getByRole("button", {
      name: "Show remaining 4 countries (3.98%)",
    });
    expect(expander.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(expander);

    expect(screen.getByRole("button", { name: /Show the top 8 countries only/ })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Show the top 8 countries only/ }).getAttribute("aria-expanded"),
    ).toBe("true");
    expect(bodyRowKeys()).toEqual(GEOGRAPHY_12.map((r) => r.key));

    fireEvent.click(screen.getByRole("button", { name: /Show the top 8 countries only/ }));
    expect(bodyRowKeys()).toHaveLength(9);
  });

  it("keeps the totals identity: the folded list sums to exactly the unfolded list", async () => {
    await openTab("Geography");

    const foldedWeight = GEOGRAPHY_12.slice(0, 8).reduce((sum, r) => sum + r.weight_pct, 0) + 3.98;
    const fullWeight = GEOGRAPHY_12.reduce((sum, r) => sum + r.weight_pct, 0);
    expect(foldedWeight).toBeCloseTo(fullWeight, 9);

    const foldedCents =
      GEOGRAPHY_12.slice(0, 8).reduce((sum, r) => sum + cents(r.market_value_cad), 0) +
      cents("57502.48");
    expect(foldedCents).toBe(GEOGRAPHY_12.reduce((sum, r) => sum + cents(r.market_value_cad), 0));

    // The donut's centre carries the SERVER's total — the fold changes the
    // slices, never the number they must add up to (M130's anti-fabrication
    // pin, applied to the exposure slices).
    const donut = screen.getByRole("img", { name: /Exposure breakdown — categorical breakdown/ });
    expect(donut.getAttribute("aria-label")).toContain(
      `total ${formatCadCompact(Number(totalOf(GEOGRAPHY_12)))}`,
    );
  });

  it("remembers the expansion for the session (M139's sessionStorage pattern)", async () => {
    const first = renderPage(<Investments />);
    await selectTab("Geography");
    fireEvent.click(screen.getByRole("button", { name: "Show remaining 4 countries (3.98%)" }));
    expect(window.sessionStorage.getItem("fintrack.exposure-expanded.v1")).toBe(
      '["geography"]',
    );

    // Re-entering the page in the same tab comes back expanded.
    first.unmount();
    renderPage(<Investments />);
    await selectTab("Geography");

    expect(bodyRowKeys()).toEqual(GEOGRAPHY_12.map((r) => r.key));
  });

  it("applies the SAME rule to every dimension, and to companies at 10", async () => {
    renderPage(<Investments />);

    const sectors = GEOGRAPHY_12.map((r, i) => row(`Sector ${i}`, r.weight_pct, r.market_value_cad));
    h.getBreakdown.mockResolvedValue(payload("sector", sectors));
    await selectTab("Sector");
    expect(screen.getByRole("button", { name: "Show remaining 4 sectors (3.98%)" })).toBeTruthy();
    expect(bodyRowKeys()[8]).toBe("Other");

    const companies = GEOGRAPHY_12.map((r) => row(r.key, r.weight_pct, r.market_value_cad));
    h.getBreakdown.mockResolvedValue(payload("company", companies));
    await selectTab("Companies");
    expect(screen.getByRole("button", { name: "Show remaining 2 companies (1.61%)" })).toBeTruthy();
    expect(bodyRowKeys()).toHaveLength(11); // 10 companies + Other

    const assetClasses = GEOGRAPHY_12.map((r) => row(r.key, r.weight_pct, r.market_value_cad));
    h.getBreakdown.mockResolvedValue(payload("asset_class", assetClasses));
    await selectTab("Asset class");
    expect(
      screen.getByRole("button", { name: "Show remaining 4 asset classes (3.98%)" }),
    ).toBeTruthy();
  });

  it("never folds Unclassified — the honesty bucket stays on screen at any rank", async () => {
    const withUnclassified: BreakdownRow[] = [
      ...GEOGRAPHY_12.slice(0, 10),
      row("Unclassified", 0.7, "10060.12"),
      row("Belgium", 0.71, "10261.47"),
    ];
    h.getBreakdown.mockResolvedValue(payload("geography", withUnclassified));

    await openTab("Geography");

    const keys = bodyRowKeys();
    expect(keys).toContain("Unclassified");
    // 12 countries + Unclassified = 13 rows, folded at 8: 8 + Unclassified + Other.
    expect(keys).toHaveLength(10);
    expect(keys[keys.length - 1]).toBe("Other");
  });

  it("folds nothing when the list already fits", async () => {
    h.getBreakdown.mockResolvedValue(payload("geography", GEOGRAPHY_12.slice(0, 6)));

    await openTab("Geography");

    expect(bodyRowKeys()).toEqual(GEOGRAPHY_12.slice(0, 6).map((r) => r.key));
    expect(tableBody().queryByRole("button", { name: /Show remaining/ })).toBeNull();
  });
});

// M167 — the Geography view's world choropleth (docs/09 §Exposure visuals).
//
// What is pinned here:
//   * the map is ONE labelled group and every shaded country is a named,
//     focusable control — the a11y floor the axe sweep (docs/13) then checks
//     against the deployed build;
//   * a country with no exposure is the same neutral land as a country the
//     payload never mentioned (no invented zeros), and a bucket the geometry
//     CANNOT place is printed under the map instead of being dropped;
//   * activating a country drives the TABLE (scroll + highlight), and unfolds
//     the list first when the fold is what hides that country;
//   * the map's control set is exactly the buckets the geometry can place —
//     M169's table-side drill buttons (including `Drill into Unclassified`)
//     are table content, and the map group holds none of them;
//   * the readout inherits M139's mask like every other money surface.
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
import type { BreakdownRow } from "../src/api/portfolio";
import { CHART_PALETTES } from "../src/components/charts/palette";
import { clearExpandedDimensions } from "../src/lib/exposureFold";
import { MASK_TEXT, setMasked } from "../src/lib/mask";

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

/** Mark's shape, plus the two honest absences the map has to handle. */
const GEOGRAPHY: BreakdownRow[] = [
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
  // A REGION the holdings column can emit — no country shape exists.
  row("North America", 0.9, "12999.9"),
  // Not a country at all: the pipeline's own honesty bucket.
  row("Unclassified", 0.71, "10261.47"),
];

const totalOf = (rows: readonly BreakdownRow[]) =>
  (
    rows.reduce((sum, r) => sum + Math.round(Number(r.market_value_cad) * 100), 0) / 100
  ).toFixed(2);

async function openGeography() {
  renderPage(<Investments />);
  fireEvent.click(await screen.findByRole("tab", { name: "Geography" }));
  await screen.findByLabelText("Exposure breakdown table");
  // The choropleth rides its own chunk behind `lazy` (docs/09 §Exposure
  // visuals), so wait for IT rather than assuming it landed with the table.
  await screen.findByRole("group", { name: /Exposure by country/ });
}

function mapCountry(iso3: string): SVGElement {
  const element = document.querySelector<SVGElement>(`[data-country="${iso3}"]`);
  if (!element) throw new Error(`no map shape for ${iso3}`);
  return element;
}

function bodyRowKeys(): string[] {
  return [...document.querySelectorAll<HTMLElement>("[data-exposure-row]")].map(
    (tr) => tr.dataset.exposureRow ?? "",
  );
}

beforeEach(() => {
  clearExpandedDimensions();
  setMasked(false);
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
    items: GEOGRAPHY,
    total_cad: totalOf(GEOGRAPHY),
    unclassified_cad: "10261.47",
  });
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  h.getPositions.mockResolvedValue({ items: [], total: 0 });
  h.getPriceHistory.mockResolvedValue({ items: [], security: null });
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  clearExpandedDimensions();
  setMasked(false);
  window.localStorage.clear();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("M167 — the world choropleth", () => {
  it("renders one labelled group that says what the map says", async () => {
    await openGeography();

    const map = screen.getByRole("group", { name: /Exposure by country — world choropleth/ });
    // 10 countries place on the map; the region bucket cannot.
    expect(map.getAttribute("aria-label")).toContain("10 points");
    expect(map.getAttribute("aria-label")).toContain("1 bucket outside the map");
  });

  it("labels every shaded country with its name and weight, and nothing else", async () => {
    await openGeography();

    expect(
      screen.getByRole("button", { name: "United States, 44.70% of exposure" }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Canada, 20.49% of exposure" })).toBeTruthy();

    // The map's OWN group is the whole control set: exactly the ten buckets
    // the geometry can place, each named `<country>, <weight>% of exposure`,
    // and no eleventh control. This is scoped on purpose — a page-wide query
    // for `Unclassified` finds M169's table-side drill button (`Drill into
    // Unclassified`), which is legitimate content of the TABLE, never a place
    // on the map.
    const map = screen.getByRole("group", { name: /Exposure by country — world choropleth/ });
    const mapControls = within(map)
      .getAllByRole("button")
      .map((control) => control.getAttribute("aria-label"))
      .sort();
    expect(mapControls).toEqual(
      [
        "Australia, 2.50% of exposure",
        "Canada, 20.49% of exposure",
        "France, 3.30% of exposure",
        "Germany, 4.20% of exposure",
        "India, 1.29% of exposure",
        "Japan, 7.53% of exposure",
        "Netherlands, 1.08% of exposure",
        "Switzerland, 1.90% of exposure",
        "United Kingdom, 5.10% of exposure",
        "United States, 44.70% of exposure",
      ].sort(),
    );

    // Unclassified is not a country, and a region has no shape: neither may
    // claim to be a place on the map. Both stay readable in text — the region
    // prints under the map (pinned below), the honesty bucket is a table row
    // whose only control is M169's collapsed drill.
    expect(within(map).queryByRole("button", { name: /Unclassified/ })).toBeNull();
    expect(within(map).queryByRole("button", { name: /North America/ })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Drill into Unclassified" }).getAttribute("aria-expanded"),
    ).toBe("false");

    // Every other country is presentational background — one image, not 170
    // stray nodes for a screen reader to walk.
    const brazil = mapCountry("BRA");
    expect(brazil.getAttribute("aria-hidden")).toBe("true");
    expect(brazil.getAttribute("role")).toBeNull();
  });

  it("draws unexposed land as the neutral surface, never as a zero", async () => {
    await openGeography();

    // jsdom resolves the dark theme (no stored preference, matchMedia false).
    expect(mapCountry("BRA").getAttribute("fill")).toBe(CHART_PALETTES.dark.SURFACE);
    expect(mapCountry("USA").getAttribute("fill")).not.toBe(CHART_PALETTES.dark.SURFACE);
    // Shading is the donut's own accent, mixed — the house palette, not a
    // stray colour shipped inside a component. (Hexes re-derived by the M185
    // Ledger token re-skin; same ramp, new accent.)
    expect(mapCountry("USA").getAttribute("fill")).toBe("#79a9ff"); // 44.7% → top step
    expect(mapCountry("CAN").getAttribute("fill")).toBe("#75a3f6"); // 20.49% → 10–25%
  });

  it("activating a country drives the table: highlight + readout, mask-aware", async () => {
    await openGeography();

    fireEvent.click(screen.getByRole("button", { name: "Canada, 20.49% of exposure" }));

    expect(screen.getByTestId("exposure-map-readout").textContent).toContain("Canada");
    expect(screen.getByTestId("exposure-map-readout").textContent).toContain("20.49%");
    expect(screen.getByTestId("exposure-map-readout").textContent).toContain("$295,961.03");

    const canadaRow = document.querySelector<HTMLElement>('[data-exposure-row="Canada"]');
    expect(canadaRow?.className).toContain("ring-accent");
  });

  it("unfolds the list when the fold is what hides the country you tapped", async () => {
    await openGeography();

    // Netherlands is rank 10 of 10 placeable countries: folded away.
    expect(bodyRowKeys()).not.toContain("Netherlands");
    expect(
      screen.getByRole("button", { name: "Show remaining 3 countries (3.27%)" }).getAttribute(
        "aria-expanded",
      ),
    ).toBe("false");

    fireEvent.click(screen.getByRole("button", { name: "Netherlands, 1.08% of exposure" }));

    expect(bodyRowKeys()).toContain("Netherlands");
    expect(
      screen.getByRole("button", { name: /Show the top 8 countries only/ }).getAttribute(
        "aria-expanded",
      ),
    ).toBe("true");
    expect(
      document.querySelector<HTMLElement>('[data-exposure-row="Netherlands"]')?.className,
    ).toContain("ring-accent");
  });

  it("is keyboard-operable: focus reads the country, Enter selects it", async () => {
    await openGeography();

    const japan = screen.getByRole("button", { name: "Japan, 7.53% of exposure" });
    fireEvent.focus(japan);
    expect(screen.getByTestId("exposure-map-readout").textContent).toContain("Japan");

    fireEvent.keyDown(japan, { key: "Enter" });
    expect(
      document.querySelector<HTMLElement>('[data-exposure-row="Japan"]')?.className,
    ).toContain("ring-accent");

    fireEvent.keyDown(japan, { key: " " });
    expect(screen.getByTestId("exposure-map-readout").textContent).toContain("Japan");
  });

  it("inherits M139's mask — percentages stay, dollars become bullets", async () => {
    setMasked(true);
    await openGeography();

    fireEvent.click(screen.getByRole("button", { name: "Canada, 20.49% of exposure" }));

    const readout = screen.getByTestId("exposure-map-readout");
    expect(readout.textContent).toContain("Canada");
    expect(readout.textContent).toContain("20.49%");
    expect(readout.textContent).toContain(MASK_TEXT);
    expect(readout.textContent).not.toContain("$295,961.03");
    // The table it points at masks the same way — one mask flag, ambient.
    expect(screen.queryByText("$295,961.03")).toBeNull();
  });

  it("prints the weight it cannot draw, instead of dropping it", async () => {
    await openGeography();

    const honesty = screen.getByTestId("exposure-map-unplaceable");
    expect(honesty.textContent).toContain("North America (0.90%)");
    expect(honesty.textContent).toContain("no country shape");
    // And the Unclassified bucket belongs to the table, not to the map.
    expect(honesty.textContent).not.toContain("Unclassified");
  });

  it("collapses on mobile and leaves the map alone at lg", async () => {
    await openGeography();

    const toggle = screen.getByRole("button", { name: "Hide map" });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const regionId = toggle.getAttribute("aria-controls");
    expect(regionId).toBeTruthy();
    const mapRegion = document.getElementById(regionId as string) as HTMLElement;
    expect(mapRegion.className).toContain("block");

    fireEvent.click(toggle);

    const collapsed = screen.getByRole("button", { name: "Show map" });
    expect(collapsed.getAttribute("aria-expanded")).toBe("false");
    // The lg: variant keeps the map on desktop; only the phone hides it.
    expect(mapRegion.className).toContain("hidden");
    expect(mapRegion.className).toContain("lg:block");
  });
});

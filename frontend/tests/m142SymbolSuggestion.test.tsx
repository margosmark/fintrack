// M142 — the typed-name → canonical-ticker suggestion on the add form
// (docs/09 §New security suggestion chip, docs/07 §Symbol search fallback).
//
// The four flows that must never regress:
//   1  the probe resolves WHILE typing (debounced, once per settled value)
//      and OFFERS the canonical listing — "Did you mean APTX.TO — APOTEX
//      HEALTH CORP (Toronto, CAD)?" — instead of taking over the field;
//   2  accepting writes the canonical symbol + every autofilled field in one
//      click, and the create posts THAT listing (never the typed name);
//   3  several listings open the pick-one chooser, and a pick applies the
//      chosen listing directly (M143: a candidate IS create-ready, so the
//      chooser is never re-asked the question it just answered);
//   4  continuing to type clears the offer, and a MISS while typing is
//      silent (W11 pin 7's loud warning belongs to the explicit Look up).
//
// Harness mirrors m126PriceHistory.test.tsx: the real Investments page, the
// real query client, mocked domain APIs.
import type { ReactElement } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../src/api/client";
import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";

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
    lookupSecurity: vi.fn(),
    createSecurity: vi.fn(),
    user,
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
        (a: { type: keyof typeof actual.INVESTMENT_ACCOUNT_TYPES }) =>
          actual.INVESTMENT_ACCOUNT_TYPES[a.type] === true,
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
  return {
    ...actual,
    listSecurities: h.listSecurities,
    lookupSecurity: h.lookupSecurity,
    createSecurity: h.createSecurity,
  };
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
vi.stubGlobal("EventSource", FakeEventSource);

// The live Yahoo answer for q=Apotex (probed 2026-10-10): TWO listings, so
// the real user flow is the chooser; the single-hit shape is row 1 alone.
const APOTEX_TWO = {
  match: "ambiguous" as const,
  symbol: null,
  name: null,
  kind: null,
  listing_currency: null,
  exchange: null,
  exchange_display: null,
  candidates: [
    {
      symbol: "APTX.TO",
      name: "APOTEX HEALTH CORP",
      kind: "stock" as const,
      listing_currency: "CAD",
      exchange: "TSX",
      exchange_display: "Toronto",
    },
    {
      symbol: "APTX.NE",
      name: "APOTEX HEALTH CORP",
      kind: "stock" as const,
      listing_currency: "CAD",
      exchange: "NEO",
      exchange_display: "NEO",
    },
  ],
};
const APTX_TO = {
  match: "direct" as const,
  symbol: "APTX.TO",
  name: "APOTEX HEALTH CORP",
  kind: "stock" as const,
  listing_currency: "CAD",
  exchange: "TSX",
  exchange_display: "Toronto",
  candidates: [],
};

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

/** The New security form's symbol field (placeholder is the pin). */
function symbolInput(): HTMLInputElement {
  return screen.getByPlaceholderText("XEQT") as HTMLInputElement;
}

/** The form lives behind the trade card's `+ New security` toggle. */
function openForm(): void {
  fireEvent.click(screen.getByRole("button", { name: "+ New security" }));
}

/** Mount the page and open the add form the way a user does. */
async function renderForm(): Promise<void> {
  renderPage(<Investments />);
  await screen.findByRole("button", { name: "+ New security" });
  openForm();
}

async function type(value: string): Promise<void> {
  fireEvent.change(symbolInput(), { target: { value } });
  // The debounce is 600 ms; give it (and the mutation) real time to land.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 750));
  });
}

beforeEach(() => {
  h.restoreSession.mockResolvedValue({
    access_token: "token",
    token_type: "bearer",
    user: h.user,
  });
  h.me.mockResolvedValue(h.user);
  h.listAccounts.mockResolvedValue(h.page([]));
  h.listCategories.mockResolvedValue(h.page([]));
  h.getPositions.mockResolvedValue({ items: [], total: 0 });
  h.getPortfolioValueHistory.mockResolvedValue({ items: [] });
  h.getExposure.mockResolvedValue({ items: [], total: 0 });
  h.getBreakdown.mockResolvedValue({
    dimension: "sector",
    items: [],
    total_cad: "0",
    unclassified_cad: "0",
  });
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  h.createSecurity.mockResolvedValue({ id: 41, symbol: "APTX.TO" });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("M142 — the add form's suggestion chip", () => {
  it("offers the canonical listing the typed name resolved to", async () => {
    h.lookupSecurity.mockResolvedValue(APTX_TO);
    await renderForm();
    await type("APOTEX");

    // One probe for the settled value, with the string the user typed.
    expect(h.lookupSecurity).toHaveBeenCalledTimes(1);
    expect(h.lookupSecurity).toHaveBeenCalledWith("APOTEX");

    const chip = await screen.findByTestId("symbol-suggestion");
    expect(chip.textContent).toContain("Did you mean");
    expect(chip.textContent).toContain("APTX.TO");
    expect(chip.textContent).toContain("APOTEX HEALTH CORP (Toronto, CAD)");
    // The typed string stays in the field until the user accepts.
    expect(symbolInput().value).toBe("APOTEX");
  });

  it("accepting writes the canonical symbol and every autofilled field", async () => {
    h.lookupSecurity.mockResolvedValue(APTX_TO);
    await renderForm();
    await type("APOTEX");

    fireEvent.click(await screen.findByTestId("accept-symbol-suggestion"));

    expect(symbolInput().value).toBe("APTX.TO");
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("APOTEX HEALTH CORP");
    expect((screen.getByLabelText("Currency") as HTMLInputElement).value).toBe("CAD");
    expect((screen.getByLabelText("Exchange") as HTMLInputElement).value).toBe("TSX");
    expect(screen.queryByTestId("symbol-suggestion")).toBeNull();

    // Nothing is stored by the offer — the submit is, and it posts the
    // CANONICAL listing, so the `.TO` FX/listing-currency seams apply.
    expect(h.createSecurity).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Create security" }));
    await waitFor(() => expect(h.createSecurity).toHaveBeenCalledTimes(1));
    expect(h.createSecurity.mock.calls[0][0]).toMatchObject({
      symbol: "APTX.TO",
      listing_currency: "CAD",
      exchange: "TSX",
    });
  });

  it("opens the pick-one chooser when several listings match, and a pick applies it", async () => {
    h.lookupSecurity.mockResolvedValue(APOTEX_TWO);
    await renderForm();
    await type("APOTEX");

    const chooser = await screen.findByTestId("symbol-candidates");
    expect(chooser.textContent).toContain("Several listings match");
    expect(screen.getByTestId("symbol-candidate-APTX.TO")).toBeTruthy();
    expect(screen.getByTestId("symbol-candidate-APTX.NE")).toBeTruthy();
    expect(screen.queryByTestId("symbol-suggestion")).toBeNull();

    fireEvent.click(screen.getByTestId("symbol-candidate-APTX.TO"));

    // M143: the candidate IS create-ready, so the pick never goes back to
    // the endpoint — re-resolving a bare-vs-`.TO` collision would only hand
    // the form the SAME chooser the user just answered (docs/07).
    expect(h.lookupSecurity).toHaveBeenCalledTimes(1);
    expect(symbolInput().value).toBe("APTX.TO");
    expect((screen.getByLabelText("Currency") as HTMLInputElement).value).toBe("CAD");
    expect((screen.getByLabelText("Exchange") as HTMLInputElement).value).toBe("TSX");
    expect(screen.queryByTestId("symbol-candidates")).toBeNull();
  });

  it("clears the offer when the user keeps typing instead", async () => {
    h.lookupSecurity.mockResolvedValue(APTX_TO);
    await renderForm();
    await type("APOTEX");
    await screen.findByTestId("symbol-suggestion");

    fireEvent.change(symbolInput(), { target: { value: "APOTEX HEALT" } });

    expect(screen.queryByTestId("symbol-suggestion")).toBeNull();
    expect(screen.queryByTestId("accept-symbol-suggestion")).toBeNull();
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("");
  });

  it("stays silent on a miss while typing (the button owns the loud warning)", async () => {
    h.lookupSecurity.mockRejectedValue(
      new ApiError(404, "No market data available for symbol 'PRIVCO'", "lookup_unavailable"),
    );
    await renderForm();
    await type("PRIVCO");

    expect(h.lookupSecurity).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("symbol-suggestion")).toBeNull();
    expect(screen.queryByTestId("lookup-warning")).toBeNull();
    // The form is still usable — a miss never blocks manual creation.
    expect(screen.getByRole("button", { name: "Create security" })).toBeTruthy();
  });

  it("keeps the new-security form OUT of the trade form (no nested <form>)", async () => {
    // A nested <form> is invalid HTML: a real browser does not deliver the
    // inner submit to React's handler, so "Create security" performed a
    // NATIVE submission of the outer form — the page reloaded and nothing
    // was created. Caught by the live probe, pinned here.
    await renderForm();

    expect(document.querySelector('[data-testid="new-security-form"] form')).not.toBeNull();
    expect(document.querySelector("form form")).toBeNull();
  });

  it("keeps the explicit Look up button's persistent warning on a miss", async () => {
    h.lookupSecurity.mockRejectedValue(
      new ApiError(404, "No market data available for symbol 'PRIVCO'", "lookup_unavailable"),
    );
    await renderForm();
    fireEvent.change(symbolInput(), { target: { value: "PRIVCO" } });

    fireEvent.click(screen.getByRole("button", { name: "Look up" }));

    const warning = await screen.findByTestId("lookup-warning");
    expect(warning.textContent).toContain("PRIVCO");
  });
});

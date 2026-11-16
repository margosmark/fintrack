// M143 — the bare-ticker collision on the add form
// (docs/09 §Candidate picker, docs/07 §Candidate resolution).
//
// The flows that must never regress:
//   1  a dual-listed ticker (`VGRO` exists on NYSEArca AND `VGRO.TO` on the
//      TSX) opens the chooser with BOTH sides, each showing its CURRENCY —
//      never a silent bare-first pick;
//   2  a pick applies the chosen listing DIRECTLY (one lookup total): a
//      candidate is create-ready, so re-resolving would only re-ask the
//      collision the chooser just answered;
//   3  the create posts exactly the chosen listing, currency and venue;
//   4  a miss while typing leaves NO guessed currency in the form — the
//      unresolved row is honest about what is unknown.
//
// Harness mirrors m142SymbolSuggestion.test.tsx: the real Investments page,
// the real query client, mocked domain APIs.
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
    checkQuote: vi.fn(),
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
    checkQuote: h.checkQuote,
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

// LIVE-PROBED 2026-10-11: `VGRO` answers a REAL US ETF and `VGRO.TO` a real
// TSX ETF — different issuers, different currencies. That is security 294.
const VGRO_COLLISION = {
  match: "ambiguous" as const,
  symbol: null,
  name: null,
  kind: null,
  listing_currency: null,
  exchange: null,
  exchange_display: null,
  candidates: [
    {
      symbol: "VGRO",
      name: "Virtus Silvant Growth Opportuni",
      kind: "etf" as const,
      listing_currency: "USD",
      exchange: "PCX",
      exchange_display: "NYSEArca",
    },
    {
      symbol: "VGRO.TO",
      name: "VANGUARD GROWTH ETF PORTFOLIO",
      kind: "etf" as const,
      listing_currency: "CAD",
      exchange: "TSX",
      exchange_display: "Toronto",
    },
  ],
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

function symbolInput(): HTMLInputElement {
  return screen.getByPlaceholderText("XEQT") as HTMLInputElement;
}

async function renderForm(): Promise<void> {
  renderPage(<Investments />);
  await screen.findByRole("button", { name: "+ New security" });
  fireEvent.click(screen.getByRole("button", { name: "+ New security" }));
}

async function type(value: string): Promise<void> {
  fireEvent.change(symbolInput(), { target: { value } });
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
  h.createSecurity.mockResolvedValue({ id: 294, symbol: "VGRO.TO" });
  h.checkQuote.mockResolvedValue({ symbol: "VGRO", quote_found: true, reason: null });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("M143 — the dual-market collision chooser", () => {
  it("shows BOTH markets with their currencies instead of picking bare-first", async () => {
    h.lookupSecurity.mockResolvedValue(VGRO_COLLISION);
    await renderForm();
    await type("VGRO");

    const chooser = await screen.findByTestId("symbol-candidates");
    expect(chooser.textContent).toContain("Several listings match");
    expect(screen.getByTestId("symbol-candidate-VGRO")).toBeTruthy();
    expect(screen.getByTestId("symbol-candidate-VGRO.TO")).toBeTruthy();

    // The whole question is the market + currency, so both are spelled out.
    expect(chooser.textContent).toContain("Virtus Silvant Growth Opportuni (NYSEArca, USD)");
    expect(chooser.textContent).toContain("VANGUARD GROWTH ETF PORTFOLIO (Toronto, CAD)");
    // No silent autofill next to it — the choice IS the answer.
    expect(screen.queryByTestId("symbol-suggestion")).toBeNull();
    expect(h.createSecurity).not.toHaveBeenCalled();
  });

  it("applies the CAD pick directly and posts that listing — no 2nd lookup", async () => {
    h.lookupSecurity.mockResolvedValue(VGRO_COLLISION);
    await renderForm();
    await type("VGRO");

    fireEvent.click(await screen.findByTestId("symbol-candidate-VGRO.TO"));

    expect(symbolInput().value).toBe("VGRO.TO");
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe(
      "VANGUARD GROWTH ETF PORTFOLIO",
    );
    expect((screen.getByLabelText("Currency") as HTMLInputElement).value).toBe("CAD");
    expect((screen.getByLabelText("Exchange") as HTMLInputElement).value).toBe("TSX");
    expect(screen.queryByTestId("symbol-candidates")).toBeNull();
    // One probe, one answer. A second lookup would only re-open the very
    // collision the user just resolved.
    expect(h.lookupSecurity).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Create security" }));
    await waitFor(() => expect(h.createSecurity).toHaveBeenCalledTimes(1));
    expect(h.createSecurity.mock.calls[0][0]).toMatchObject({
      symbol: "VGRO.TO",
      name: "VANGUARD GROWTH ETF PORTFOLIO",
      listing_currency: "CAD",
      exchange: "TSX",
    });
  });

  it("the US pick is equally available — the bare side is never hidden", async () => {
    h.lookupSecurity.mockResolvedValue(VGRO_COLLISION);
    await renderForm();
    await type("VGRO");

    fireEvent.click(await screen.findByTestId("symbol-candidate-VGRO"));
    expect(symbolInput().value).toBe("VGRO");
    expect((screen.getByLabelText("Currency") as HTMLInputElement).value).toBe("USD");
    expect((screen.getByLabelText("Exchange") as HTMLInputElement).value).toBe("PCX");
    expect(h.lookupSecurity).toHaveBeenCalledTimes(1);
  });

  it("a miss leaves NO guessed currency — the row is created unresolved", async () => {
    h.lookupSecurity.mockRejectedValue(
      new ApiError(404, "No market data available for symbol 'ZZUNLISTED'", "lookup_unavailable"),
    );
    h.checkQuote.mockResolvedValue({ symbol: "ZZUNLISTED", quote_found: false, reason: null });
    await renderForm();
    await type("ZZUNLISTED");

    // The old "CAD" default made an unresolved row indistinguishable from a
    // resolved one (docs/07 §Unresolved symbols).
    expect((screen.getByLabelText("Currency") as HTMLInputElement).value).toBe("");

    // A name is still required — an unresolved row is a real row, only its
    // market identity is unknown.
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "ZZUNLISTED" } });
    fireEvent.click(screen.getByRole("button", { name: "Create security" }));
    // M141's gate: no quote found -> the explicit "Add anyway" second click.
    const warning = await screen.findByTestId("quote-warning");
    expect(warning.textContent).toContain("ZZUNLISTED");
    fireEvent.click(screen.getByRole("button", { name: "Add anyway" }));
    await waitFor(() => expect(h.createSecurity).toHaveBeenCalledTimes(1));
    expect(h.createSecurity.mock.calls[0][0]).toMatchObject({
      symbol: "ZZUNLISTED",
      listing_currency: "",
    });
  });
});

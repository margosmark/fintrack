// M103 — channel C's "observable effect" contract, pinned at the control.
//
// `scripts/a11y/keywalk.py` fails a click that produces NO observable effect
// and classifies one that produces something other than a dialog as
// `non-dialog-surface`. Its original four checks (dialog / navigation /
// visible form controls / live region) could not see `/investments`'
// empty-state "Add a symbol": that control's whole job is to move focus into
// the watchlist's security combobox, which mounts a `role="listbox"` — so a
// working button was reported as `no-effect`.
//
// This test pins the behaviour the harness now relies on: the click moves
// focus (identity, not just a descriptor), flips `aria-expanded`, and renders
// the option list. Remove the onClick in `Investments.tsx` and this fails
// before the harness has to discover it again.
//
// Harness mirrors priceStaleStream.test.tsx (real page, mocked domain APIs).
import type { ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
  const brokerage = {
    id: 1,
    institution_id: null,
    name: "Margin account",
    type: "brokerage_non_registered",
    currency: "CAD",
    taxable: true,
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
    getWatchlist: vi.fn(),
    listSecurities: vi.fn(),
    listTrades: vi.fn(),
    getDividendSuggestions: vi.fn(),
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
    // The real hook calls `listAccounts` through accounts.ts's OWN module
    // binding, which a vi.mock does not intercept — rebuild it on the mock.
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
  return { ...actual, getPositions: h.getPositions, getPortfolioValueHistory: h.getPortfolioValueHistory };
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

/** Minimal EventSource stand-in (usePriceStream.test.ts). */
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
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
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

const WATCH_INPUT = 'input[placeholder="Add a symbol to follow…"]';

beforeEach(() => {
  FakeEventSource.instances = [];
  h.restoreSession.mockResolvedValue({
    access_token: "token",
    token_type: "bearer",
    user: h.user,
  });
  h.me.mockResolvedValue(h.user);
  h.listAccounts.mockResolvedValue(h.page([h.brokerage]));
  h.listCategories.mockResolvedValue(h.page([]));
  h.getPositions.mockResolvedValue({ items: [], total: 0 });
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue(
    h.page([
      {
        id: 7,
        symbol: "RY.TO",
        name: "Royal Bank of Canada",
        kind: "stock",
        listing_currency: "CAD",
        exchange: "TSX",
      },
      {
        id: 9,
        symbol: "NVDA",
        name: "NVIDIA Corporation",
        kind: "stock",
        listing_currency: "USD",
        exchange: "NASDAQ",
      },
    ]),
  );
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("/investments empty-state \"Add a symbol\" (M103, keywalk channel C)", () => {
  it("is an enabled control whose click moves focus into the watchlist combobox", async () => {
    renderPage(<Investments />);

    // The empty watchlist renders the 3-part empty state with its one action.
    const actions = await screen.findAllByTestId("empty-state-action");
    const action = actions.find((node) => node.textContent?.includes("Add a symbol"));
    expect(action).toBeTruthy();
    const button = action?.querySelector("button");
    expect(button).not.toBeNull();
    expect(button?.disabled).toBe(false);

    const input = document.querySelector(WATCH_INPUT) as HTMLInputElement | null;
    expect(input).not.toBeNull();
    expect(input?.getAttribute("aria-expanded")).toBe("false");
    // keywalk's heuristic is identity-based: the click itself must move focus.
    expect(document.activeElement).not.toBe(input);
    const elementsBefore = document.querySelectorAll("body *").length;

    fireEvent.click(button as HTMLButtonElement);

    await waitFor(() => {
      expect(document.activeElement).toBe(input);
      expect(input?.getAttribute("aria-expanded")).toBe("true");
      // The surface it opens carries options — not just focus.
      const listbox = document.querySelector('[role="listbox"]');
      expect(listbox).not.toBeNull();
      expect(document.querySelectorAll('[role="option"]').length).toBe(2);
    });
    // …and it adds elements, which is the harness's second fallback check.
    expect(document.querySelectorAll("body *").length).toBeGreaterThan(elementsBefore);
  });

  it("keeps the listbox closed when the control is untouched (no idle false effect)", async () => {
    renderPage(<Investments />);
    await screen.findAllByTestId("empty-state-action");

    expect(document.querySelector('[role="listbox"]')).toBeNull();
    expect(document.querySelector(WATCH_INPUT)?.getAttribute("aria-expanded")).toBe("false");
  });

  it("closes the surface it opened with Escape, focus staying in the field", async () => {
    renderPage(<Investments />);
    const actions = await screen.findAllByTestId("empty-state-action");
    const button = actions
      .find((node) => node.textContent?.includes("Add a symbol"))
      ?.querySelector("button");
    expect(button).not.toBeNull();
    fireEvent.click(button as HTMLButtonElement);

    const input = await waitFor(() => {
      const field = document.querySelector(WATCH_INPUT) as HTMLInputElement;
      expect(field.getAttribute("aria-expanded")).toBe("true");
      expect(document.querySelector('[role="listbox"]')).not.toBeNull();
      return field;
    });

    // jsdom does not turn Enter/Space on a button into a click, so the
    // activation itself is covered by the click test above; what must hold
    // here is that the surface it opens is dismissable and leaves focus in a
    // sane place rather than stranding it in a detached list.
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => {
      expect(document.querySelector('[role="listbox"]')).toBeNull();
      expect(input.getAttribute("aria-expanded")).toBe("false");
    });
    expect(document.activeElement).toBe(input);
  });
});

// M118 — marker → evidence (docs/08 §Event markers, docs/09 §Marker → row).
//
// The whole point of a marker is the line it draws back to the ledger: click
// the pin (or its keyboard twin, the `Show` button in the data table) and the
// account's Activity table must land on THAT row — narrowed to the event's own
// day first, so an old row cannot sit pages away behind the default sort, and
// ringed so the eye knows where it went.
//
// This file pins the wiring no component-level test could catch: the request
// that comes back must carry the narrowed dates, and the row must be marked
// current once it renders.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({
  account: {
    id: 731,
    institution_id: null,
    name: "TFSA",
    type: "tfsa",
    currency: "CAD",
    taxable: false,
    llm_autopilot: false,
    cash_balance: "0.00",
    investment_value: "0.00",
    total_value: "0.00",
    starting_balance: "0.00",
    name_cipher: null,
    last_sync_at: null,
    provider_status: null,
    pending_import_count: 0,
  },
  txn: {
    id: 456,
    account_id: 731,
    date: "2026-01-05",
    posting_date: null,
    raw_description: "WEALTHSIMPLE EFT",
    normalized_payee: "Wealthsimple",
    amount_native: "16500.00",
    currency: "CAD",
    cad_amount: "16500.00",
    category_id: null,
    transfer_pair_id: null,
    pair_account_name: null,
    has_receipt: false,
    tags: [],
    is_recurring: false,
    recurring_cadence: null,
    recurring_next_date: null,
    reviewed_at: null,
    reviewed_by: null,
    payee_cipher: null,
    raw_desc_cipher: null,
    amount_cipher: null,
    cad_amount_cipher: null,
    payee_token: null,
    parent_split_id: null,
    is_split: false,
    note: null,
  },
  event: {
    id: "transaction:456",
    source: "transaction",
    trade_id: null,
    transaction_id: 456,
    date: "2026-01-05",
    bucket_end: "2026-01-31",
    kind: "deposit",
    direction: 1,
    amount_cad: "16500.00",
    amount_native: "16500.00",
    currency: "CAD",
    payee: "Wealthsimple",
    category_name: null,
    symbol: null,
    security_id: null,
    quantity: null,
    action: null,
  },
  listTransactions: vi.fn(),
}));

vi.mock("../src/api/accounts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/accounts")>();
  return {
    ...actual,
    getAccount: vi.fn(async () => h.account),
    getAccountLedger: vi.fn(async () => ({
      items: [],
      starting_balance: "0.00",
      current_balance: "0.00",
      total: 0,
    })),
    getAccountValueHistory: vi.fn(async () => ({
      account_id: 731,
      from: "2026-01-01",
      to: "2026-06-15",
      granularity: "month",
      requested_granularity: "month",
      currency: "CAD",
      native_currency: "CAD",
      min_event_value: "1000.00",
      has_data: true,
      buckets: [
        {
          bucket_end: "2026-01-31",
          cash_cad: "16500.00",
          positions_cad: "0.00",
          value_cad: "16500.00",
          estimated: false,
          valued: true,
        },
      ],
      events: [h.event],
    })),
  };
});
vi.mock("../src/api/transactions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/transactions")>();
  return {
    ...actual,
    listTransactions: h.listTransactions,
    getTransferSuggestions: vi.fn(async () => []),
  };
});
vi.mock("../src/api/categories", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/categories")>();
  return {
    ...actual,
    listCategories: vi.fn(async () => ({ items: [], total: 0, limit: 200, offset: 0 })),
  };
});
vi.mock("../src/api/tags", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/tags")>();
  return { ...actual, listTags: vi.fn(async () => ({ items: [], total: 0, limit: 200, offset: 0 })) };
});
// The holdings tree is not the subject here (it pulls prices and the SSE
// stream) — the marker contract lives entirely in AccountDetail + Activity.
vi.mock("../src/pages/Investments", () => ({ Investments: () => null }));

import { AccountDetail } from "../src/pages/AccountDetail";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { ToastProvider } from "../src/components/ui/Toast";

// jsdom has no matchMedia; framer-motion's reduced-motion probe calls it.
if (window.matchMedia === undefined) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={["/accounts/731"]}>
            <Routes>
              <Route path="/accounts/:accountId" element={<AccountDetail />} />
            </Routes>
          </MemoryRouter>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  h.listTransactions.mockReset();
  h.listTransactions.mockImplementation(async () => ({
    items: [h.txn],
    total: 1,
    limit: 50,
    offset: 0,
    split_parents: [],
  }));
});

describe("M118 marker → row (account value chart)", () => {
  it("narrow Activity to the event's day and marks the row current", async () => {
    h.listTransactions.mockImplementation(async () => ({
      items: [h.txn],
      total: 1,
      limit: 50,
      offset: 0,
      split_parents: [],
    }));
    renderPage();

    // The value chart is the FIRST module of the investment view.
    const chart = await screen.findByRole("img");
    expect(chart.getAttribute("aria-label")).toContain("Account value over time");

    fireEvent.click(screen.getByText("Show the numbers behind this chart"));
    fireEvent.click(
      screen.getByRole("button", { name: "Show the deposit of 2026-01-05 in the table" }),
    );

    await waitFor(() =>
      expect(h.listTransactions).toHaveBeenLastCalledWith(
        expect.objectContaining({ accountId: 731, from: "2026-01-05", to: "2026-01-05" }),
        expect.anything(),
      ),
    );
    await waitFor(() => {
      const row = document.querySelector('[data-txn-id="456"]');
      expect(row?.getAttribute("aria-current")).toBe("true");
    });
    // …and the filter bar shows WHY the table narrowed (visible, not magic):
    // both date inputs carry the event's day.
    expect(screen.getAllByDisplayValue("2026-01-05").length).toBeGreaterThanOrEqual(2);
  });

  it("lists the marker from the wire payload with its evidence line", async () => {
    renderPage();
    await screen.findByRole("img");
    fireEvent.click(screen.getByText("Show the numbers behind this chart"));
    expect(screen.getByText("deposit")).toBeTruthy();
    expect(screen.getByText("+ $16,500.00")).toBeTruthy();
  });
});

// M113 — two halves of the same fix for Mark's TFSA-import confusion: an
// import that staged correctly was INVISIBLE from the account page, so he
// abandoned the flow twice at discard/revert.
//
// Part A is the banner contract itself (count-only, both count shapes, the
// /imports link, the ≥44px targets, dismissal per account per session).
// Part B is the page wiring: `/accounts/731` for a TFSA account whose
// `GET /accounts/{id}` carries `pending_import_count: 1` must actually render
// the banner — named after the bug because the bug was the ABSENCE of this
// element, which no component-level test could have caught.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PendingImportBanner } from "../src/components/imports/PendingImportBanner";
import { AccountDetail } from "../src/pages/AccountDetail";
import { ToastProvider } from "../src/components/ui/Toast";

const BANNER = "pending-import-banner";

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
    pending_import_count: 1,
  },
  page: <T,>(items: T[]) => ({ items, total: items.length, limit: 200, offset: 0 }),
}));

vi.mock("../src/api/accounts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/accounts")>();
  return {
    ...actual,
    getAccount: vi.fn(async () => h.account),
    listAccounts: vi.fn(async () => h.page([h.account])),
    getAccountLedger: vi.fn(async () => ({
      items: [],
      starting_balance: "0.00",
      current_balance: "0.00",
      total: 0,
    })),
    // M118: the banner sits above both views, and the investment view now
    // mounts the value chart — stub its read so this test never reaches out.
    getAccountValueHistory: vi.fn(async () => ({
      account_id: 1,
      from: "2026-01-01",
      to: "2026-06-15",
      granularity: "month",
      requested_granularity: "month",
      currency: "CAD",
      native_currency: "CAD",
      min_event_value: "1000.00",
      has_data: false,
      buckets: [],
      events: [],
    })),
  };
});

vi.mock("../src/api/transactions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/transactions")>();
  return {
    ...actual,
    listTransactions: vi.fn(async () => h.page([])),
    getTransferSuggestions: vi.fn(async () => []),
  };
});

vi.mock("../src/api/tags", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/tags")>();
  return { ...actual, listTags: vi.fn(async () => h.page([])) };
});

vi.mock("../src/api/categories", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/categories")>();
  return { ...actual, listCategories: vi.fn(async () => h.page([])) };
});

// The holdings tree is not the subject here (and it pulls prices/watchlists
// this test has no fixtures for) — the banner sits ABOVE both views.
vi.mock("../src/pages/Investments", () => ({ Investments: () => null }));

function renderBanner(accountId: number, count: number, label = "TFSA") {
  return render(
    <MemoryRouter initialEntries={[`/accounts/${accountId}`]}>
      <PendingImportBanner accountId={accountId} count={count} label={label} />
    </MemoryRouter>,
  );
}

function renderAccountPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/accounts/731"]}>
          <Routes>
            <Route path="/accounts/:accountId" element={<AccountDetail />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

describe("pending-import banner (M113) — component contract", () => {
  it("Mark's case: one pending import on a TFSA account is announced with a link to /imports", () => {
    renderBanner(731, 1, "TFSA");
    const banner = screen.getByTestId(BANNER);
    expect(banner.getAttribute("role")).toBe("status");
    expect(banner.getAttribute("aria-live")).toBe("polite");
    expect(banner.textContent).toContain("1 uncommitted import for TFSA");
    const link = screen.getByRole("link", { name: /imports page/i });
    expect(link.getAttribute("href")).toBe("/imports");
    // ≥44px touch target is a class contract (jsdom has no layout engine).
    expect(link.className).toContain("min-h-[44px]");
    // Count ONLY: batch contents / internal status tokens never reach the DOM.
    expect(banner.textContent).not.toMatch(/pending_review|\.csv|batch/i);
  });

  it("counts > 1 pluralise and still link to /imports", () => {
    renderBanner(731, 3, "TFSA");
    expect(screen.getByTestId(BANNER).textContent).toContain("3 uncommitted imports for TFSA");
    expect(screen.getByRole("link", { name: /imports page/i }).getAttribute("href")).toBe(
      "/imports",
    );
  });

  it("renders nothing when nothing is pending (0 = committed/discarded/reverted/no batch)", () => {
    const { container } = renderBanner(731, 0, "TFSA");
    expect(screen.queryByTestId(BANNER)).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("Dismiss hides it for the rest of the session, and only for that account", () => {
    const first = renderBanner(731, 1, "TFSA");
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(screen.queryByTestId(BANNER)).toBeNull();
    first.unmount();

    // Remount (navigation away and back): still dismissed — sessionStorage.
    renderBanner(731, 1, "TFSA");
    expect(screen.queryByTestId(BANNER)).toBeNull();
    cleanup();

    // A different account was never dismissed.
    renderBanner(99, 1, "RRSP");
    expect(screen.getByTestId(BANNER)).not.toBeNull();
    cleanup();

    // A new tab / session clears sessionStorage: the banner comes back.
    sessionStorage.clear();
    renderBanner(731, 1, "TFSA");
    expect(screen.getByTestId(BANNER)).not.toBeNull();
  });
});

describe("pending-import banner (M113) — account page wiring (Mark's case)", () => {
  it("/accounts/731 renders the banner from Account.pending_import_count", async () => {
    renderAccountPage();
    const banner = await screen.findByTestId(BANNER, {}, { timeout: 5000 });
    expect(banner.textContent).toContain("1 uncommitted import for TFSA");
    expect(screen.getByRole("link", { name: /imports page/i }).getAttribute("href")).toBe(
      "/imports",
    );
  });

  it("/accounts/731 renders NO banner when the count is 0", async () => {
    h.account.pending_import_count = 0;
    try {
      renderAccountPage();
      await screen.findByTestId(BANNER, {}, { timeout: 200 }).catch(() => null);
      expect(screen.queryByTestId(BANNER)).toBeNull();
    } finally {
      h.account.pending_import_count = 1;
    }
  });
});

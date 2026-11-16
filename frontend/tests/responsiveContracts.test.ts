import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "../src/components/layout/AppShell";
import { SubscriptionsPanel } from "../src/components/reports/SubscriptionsPanel";
import { SSEStatusIndicator } from "../src/components/ui/SSEStatusIndicator";
import { TransactionEditor } from "../src/components/transactions/TransactionEditor";

vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  getRecurringCandidates: vi.fn(),
  suppressRecurringGroup: vi.fn(),
}));

vi.mock("../src/api/tags", () => ({
  listTags: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 }),
  createTag: vi.fn(),
}));

vi.mock("../src/api/accounts", () => ({
  listAccounts: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 }),
}));

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({
    user: { id: "test-user", email: "test@example.com" },
    logout: vi.fn(),
  }),
}));

vi.mock("../src/theme/ThemeProvider", () => ({
  useTheme: () => ({ theme: "dark", toggleTheme: vi.fn() }),
}));

vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }),
});

import { getRecurringCandidates } from "../src/api/transactions";

/** M138: the card is fed by the STRICT detector now (docs/09). */
const subscriptionsResponse = {
  groups: [
    {
      group_key: "NETFLIX|monthly",
      dismiss_key: "NETFLIX|subscriptions",
      payee: "Netflix",
      cadence: "monthly",
      occurrence_count: 12,
      transaction_ids: [1, 2, 3],
      monthly_total: "16.99",
      first_date: "2025-09-01",
      last_date: "2026-09-01",
      next_expected_date: "2026-10-01",
    },
  ],
  combined_monthly_total: "16.99",
  suppressed_count: 0,
  window_start: "2025-09-01",
};

function renderSubscriptions() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  // M55 (docs/25): the panel's "Create bill reminder" action navigates, so it
  // needs the router context the app always has (Toast is mocked above).
  return render(
    createElement(
      MemoryRouter,
      { initialEntries: ["/dashboard"] },
      createElement(QueryClientProvider, { client }, createElement(SubscriptionsPanel)),
    ),
  );
}

function renderShell() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    createElement(
      MemoryRouter,
      { initialEntries: ["/dashboard"] },
      createElement(
        QueryClientProvider,
        { client },
        createElement(AppShell),
      ),
    ),
  );
}

function renderEditor() {
  return render(
    createElement(TransactionEditor, {
      target: null,
      accounts: [],
      categories: [],
      saving: false,
      onSave: vi.fn(),
      onClose: vi.fn(),
    }),
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("responsive and data-freshness contracts", () => {
  it("stacks subscription metadata on narrow screens", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(subscriptionsResponse as never);
    renderSubscriptions();

    const payee = await screen.findByText("Netflix");
    const row = payee.closest(".glass");

    expect(row).not.toBeNull();
    expect(row?.className).toContain("flex-col");
    expect(row?.className).toContain("sm:flex-row");
  });

  it("mounts the mobile navigation in the authenticated shell", async () => {
    renderShell();

    const nav = await screen.findByRole("navigation", { name: "Mobile navigation" });
    expect(nav).toBeTruthy();
  });

  it("renders modal surfaces outside the transformed route stacking context", async () => {
    renderShell();
    renderEditor();

    const nav = await screen.findByRole("navigation", { name: "Mobile navigation" });
    const dialog = await screen.findByRole("dialog", { name: "New transaction" });
    const backdrop = dialog.closest('[role="presentation"]');

    expect(backdrop?.parentElement).toBe(document.body);
    expect(nav.compareDocumentPosition(backdrop!)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("describes a connected stream as connected, not live", () => {
    render(
      createElement(SSEStatusIndicator, {
        status: "connected",
        lastEventAt: null,
      }),
    );

    expect(screen.getByText("Stream connected")).toBeTruthy();
    expect(screen.queryByText("Live")).toBeNull();
  });
});

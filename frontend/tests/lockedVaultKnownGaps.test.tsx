// docs/33 hardening audit — the KNOWN-RED rows, plus the explained-disabled
// exemplar they had to copy (A1's unlock hint) and A18/F4, which M136 FIXED.
//
// Taxonomy (the same cut `scripts/locked_vault_gate.py` reports):
//   works             — behaves as designed (lockedVaultDegradation.test.ts)
//   hidden-on-purpose — route removed; the client computes (the 405/404/422 rows)
//   explained-disabled— feature available, but says "unlock …" instead of
//                       silently under-delivering. A1's global-search hint is
//                       the exemplar; A2's M119 popover is the second; A18's
//                       S5 search view joined them in M136.
//   known-red         — F2: the honesty contract, still NOT shipped.
//
// A known-red row is pinned as a CHARACTERIZATION test: it asserts today's
// (defective) behaviour, so the suite stays green while the row stays red in
// the audit table. The instant the fix ships, the assertion fails — the test
// name says exactly which line to flip. That is deliberate: a silently failing
// `it.fails` would also swallow a broken harness, and this file renders two
// real pages, so a masked failure is indistinguishable from a real one.
//
// A18/F4 was exactly that tripwire and it FIRED (M136): the assertion below
// now pins the FIXED behaviour instead, and the audit row moved to WORKS.
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import type { User } from "../src/types";

const m = vi.hoisted(() => ({
  restoreSession: vi.fn<() => Promise<unknown>>(),
  logout: vi.fn<() => Promise<void>>(),
  searchGlobal: vi.fn(),
  listTransactions: vi.fn(),
  fetchAllTransactions: vi.fn(),
  getUnreviewedCount: vi.fn(),
  updateTransaction: vi.fn(),
  deleteTransaction: vi.fn(),
  setTransactionTags: vi.fn(),
  getTransactionTags: vi.fn(),
  listAccounts: vi.fn(),
  listCategories: vi.fn(),
  listTags: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, restoreSession: m.restoreSession, logout: m.logout };
});
vi.mock("../src/api/search", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/search")>()),
  searchGlobal: m.searchGlobal,
}));
vi.mock("../src/api/accounts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/accounts")>()),
  listAccounts: m.listAccounts,
}));
vi.mock("../src/api/categories", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/categories")>()),
  listCategories: m.listCategories,
}));
vi.mock("../src/api/tags", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/tags")>()),
  listTags: m.listTags,
}));
vi.mock("../src/api/notifications", () => ({
  INBOX_QUERY_KEY: ["notifications", "inbox"],
  getNotificationInbox: vi.fn().mockResolvedValue({ items: [], unread_count: 0 }),
  markNotificationRead: vi.fn().mockResolvedValue(undefined),
  markAllNotificationsRead: vi.fn().mockResolvedValue(undefined),
  unreadBadgeLabel: () => "",
}));
vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  listTransactions: m.listTransactions,
  fetchAllTransactions: m.fetchAllTransactions,
  getUnreviewedCount: m.getUnreviewedCount,
  deleteTransaction: m.deleteTransaction,
  updateTransaction: m.updateTransaction,
  setTransactionTags: m.setTransactionTags,
  getTransactionTags: m.getTransactionTags,
}));
vi.mock("../src/api/suggestions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/suggestions")>()),
  listSuggestions: vi.fn().mockResolvedValue([]),
}));
vi.mock("../src/theme/ThemeProvider", () => ({
  useTheme: () => ({ theme: "dark", toggleTheme: vi.fn() }),
}));
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  ToastProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { AuthProvider, useAuth } from "../src/auth/AuthContext";
import { AppShell } from "../src/components/layout/AppShell";
import { Transactions } from "../src/pages/Transactions";
import { isUnlocked, createVault, lockVault } from "../src/lib/vault";

// jsdom has no matchMedia; framer-motion and the shell's breakpoint ask for it.
let desktopViewport = true;
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: /min-width/.test(query) ? desktopViewport : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

const user: User = {
  id: "u1",
  email: "locked@example.com",
  name: null,
  privacy_mode: false,
  currency_tracker_enabled: true,
  llm_provider: "none",
  llm_base_url: null,
  llm_model: "",
  encryption_required: false,
};

/** A row a synced (encrypted) device wrote — cipher columns present. */
const CIPHER_TXN = {
  id: 901,
  account_id: 7,
  date: "2026-01-05",
  posting_date: null,
  raw_description: "SECRET GROCERY RUN",
  normalized_payee: "SECRET GROCERY RUN",
  amount_native: "-42.10",
  currency: "CAD",
  cad_amount: "-42.10",
  category_id: null,
  transfer_pair_id: null,
  pair_account_name: null,
  tags: [],
  payee_cipher: "v1:opaque-ciphertext",
  raw_desc_cipher: "v1:opaque-ciphertext",
  amount_cipher: "v1:opaque-ciphertext",
  cad_amount_cipher: "v1:opaque-ciphertext",
  payee_token: "v1:opaque-token",
} as never;

const page = <T,>(items: T[], total = items.length) => ({
  items,
  total,
  limit: 200,
  offset: 0,
});

function mount(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <MemoryRouter initialEntries={["/transactions?q=grocery"]}>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

/** Exposes `useAuth` so a test can drive the real logout path. */
let authProbe: { logout: () => Promise<void>; user: unknown } | null = null;
function AuthProbe() {
  authProbe = useAuth();
  return null;
}

beforeEach(async () => {
  vi.clearAllMocks();
  lockVault();
  authProbe = null;
  desktopViewport = true;
  m.restoreSession.mockResolvedValue({
    access_token: "token",
    refresh_token: "unused",
    token_type: "bearer",
    user,
  });
  m.logout.mockResolvedValue(undefined);
  m.listAccounts.mockResolvedValue(page([], 0));
  m.listCategories.mockResolvedValue(page([], 0));
  m.listTags.mockResolvedValue(page([], 0));
  m.listTransactions.mockResolvedValue(page([CIPHER_TXN], 1));
  m.fetchAllTransactions.mockResolvedValue([CIPHER_TXN]);
  m.getUnreviewedCount.mockResolvedValue(0);
  m.updateTransaction.mockResolvedValue(CIPHER_TXN);
  m.deleteTransaction.mockResolvedValue(undefined);
  m.setTransactionTags.mockResolvedValue({ items: [] });
  m.getTransactionTags.mockResolvedValue({ items: [] });
  m.searchGlobal.mockResolvedValue({
    transactions: [],
    categories: [],
    accounts: [],
    securities: [],
    tags: [],
  });
});

afterEach(() => {
  lockVault();
});

describe("A1 (explained-disabled): the global search says unlock, never a bare empty", () => {
  it("renders the 🔒 unlock hint when a locked vault hides cipher rows", async () => {
    // M137: ⌘K is ONE server query. A locked vault sends `hide_encrypted`, so
    // the answer carries no cipher rows — but it reports `withheld: 1`, the
    // measured number the hint replaces the empty state with (rule 7f).
    m.searchGlobal.mockResolvedValue({
      transactions: [],
      categories: [],
      accounts: [],
      securities: [],
      tags: [],
      withheld: 1,
    });

    mount(<AppShell />);
    fireEvent.click(await screen.findByTestId("command-palette-trigger"));
    const field = await screen.findByTestId("command-palette-input");
    fireEvent.change(field, { target: { value: "grocery" } });

    // The copy A18/F4 has to copy: it names the lock AND the way out.
    const hint = await screen.findByText(/Unlock the vault/);
    expect(hint.textContent).toContain("Settings");
    // Observed while writing this (NOT asserted — copy overlap, handed to
    // @uxbot/@qabot as a finding): the bare "No matches" <p> renders in the
    // same panel as the hint, so a locked vault reads both at once. docs/33
    // only requires the hint to be present, which is what this pins.
  });
});

describe("A18/F4 — FIXED (M136): the S5 search view announces what it withholds", () => {
  it("replaces the empty state with a measured withheld count and an inline unlock", async () => {
    // Positive control: the search view really is on screen, in search mode,
    // over the locked substrate — so what follows is about the NOTICE, not a
    // harness that rendered nothing. M137: the locked search sent
    // `hide_encrypted`, so the server withheld the one cipher row and
    // REPORTED it: items empty, total 0, withheld 1.
    m.listTransactions.mockResolvedValue({ ...page([], 0), withheld: 1 });

    mount(<Transactions />);
    await waitFor(() => expect(screen.getByText(/Similar to “grocery”/)).toBeTruthy());
    expect(isUnlocked()).toBe(false);

    // THE FIX: the notice REPLACES the empty state (docs/33 rule 7f) — the
    // old "No transactions match these filters." / "Clear all" dead end is
    // gone, because `clearAllFilters` deliberately preserves `q`.
    const notice = await screen.findByTestId("vault-locked");
    expect(notice.getAttribute("data-vault-variant")).toBe("partial");
    // Rule 7f: the lock hint REPLACES that empty state — the "Clear all"
    // action that deliberately preserves `q` is gone with it. (Asserted on the
    // copy, not on `data-testid="empty-state"`: the suggestions panel on the
    // same page renders that testid for its own "No pending suggestions".)
    expect(screen.queryByText("No transactions match these filters.")).toBeNull();

    // The count is MEASURED, never invented: the substrate the scorer read
    // holds exactly one row and the lock withholds exactly one of them.
    expect(notice.textContent).toContain("Showing 0 of 1 rows");
    expect(notice.textContent).toContain("Settings → Privacy & data");

    // The primary action genuinely changes the outcome: it unlocks the vault
    // inline (one interaction, zero navigation).
    expect(screen.getByTestId("vault-unlock-toggle")).toBeTruthy();
    // …but it is NOT auto-revealed: the passphrase field only exists once the
    // user asks for it (one interaction, focus lands in the field).
    expect(screen.queryByTestId("vault-unlock-form")).toBeNull();
  });

  it("is lock-silent when nothing is withheld — the ordinary empty state stays", async () => {
    // docs/33 rule 7a: a surface the lock does not affect carries NO lock
    // copy. A plaintext-only substrate is fully searchable while locked, so
    // an empty result there is a real empty result and the normal EmptyState
    // is the honest thing to show.
    // The server answers an empty page for this scope (no row matches
    // "grocery") and withholds NOTHING — `withheld: 0` is what keeps the
    // lock copy off a surface the lock does not affect (rule 7a).
    m.listTransactions.mockResolvedValue({ ...page([], 0), withheld: 0 });

    mount(<Transactions />);
    await waitFor(() => expect(screen.getByText(/Similar to “grocery”/)).toBeTruthy());
    await waitFor(() =>
      expect(screen.getByText("No transactions match these filters.")).toBeTruthy(),
    );
    expect(screen.queryByTestId("vault-locked")).toBeNull();
    expect(screen.queryByTestId("vault-locked-chip")).toBeNull();
  });
});

describe("F2 (known red — docs/33 §key-handling inventory, finding F2)", () => {
  it("pins today's behaviour: logout clears the session but keeps the DEK", async () => {
    await createVault("f2-probe");
    expect(isUnlocked()).toBe(true);

    mount(<AuthProbe />);
    await waitFor(() => expect(authProbe).not.toBeNull());
    expect(authProbe!.user).not.toBeNull();

    await act(async () => {
      await authProbe!.logout();
    });

    // The session is gone…
    await waitFor(() => expect(authProbe!.user).toBeNull());
    // …but the DEK, the index key and the decrypted-value cache are not:
    // `lockVault()` is only wired to the Settings Lock button.
    // When F2 ships: this line flips to `toBe(false)`.
    expect(isUnlocked()).toBe(true);
  });
});

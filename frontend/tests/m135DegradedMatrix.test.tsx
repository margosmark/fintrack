// M135 §3.4 — the regression sweep the plan made a GATE and nobody landed.
//
//   §3.4-1  degraded-mode matrix — `encryption_required` on × vault locked ×
//           vault unlocked, asserting the *stated* UI state (docs/33 rules
//           4/7/7a/7e/7f), not merely the absence of a crash. Until this file
//           no read surface had ever been rendered for an ENROLLED tenant:
//           the write half was the only parametrized one
//           (`lockedVaultWriteGates.test.tsx`), so `encryption_required: true`
//           never reached the read surfaces' own assertions.
//   §3.4-5  remedy exercise — every "unlock to …" affordance is INVOKED in the
//           test and the feature must come back. An affordance that cannot
//           change the outcome is a failure, not a pass (the canonical
//           example is `clearAllFilters`, which deliberately preserves `q`).
//
// The fixture tenant's SHAPE is `backend/app/services/locked_fixture.py`
// (M136 §0-a): rows carrying a full cipher bundle + a plaintext legacy row,
// `encryption_required` true, no envelope anywhere so every browser starts
// locked. The server half of that contract is pinned in
// `backend/tests/test_locked_fixture_tenant.py`; this file drives the CLIENT
// half — the two axes the server cannot see, because the vault is client
// state and `encryption_required` only makes the SERVER hold cipher rows.
//
// The cipher rows here are REAL: `encryptTransaction` produced them under the
// same DEK the unlocked cells unlock, so the unlocked cells prove the rows
// actually come back rather than proving nothing threw.
//
// The other three §3.4 items live elsewhere, deliberately:
//   §3.4-3 stale-render sweep  → backend/tests/test_m156_stale_render_sweep.py
//   §3.4-4a React Query keys   → tests/m135QueryKeyTenantSweep.test.tsx
//   §3.4-4b PWA service worker → tests/m114StaleChunkSelfHeal.test.ts (post-logout)
//   G6 / §3.4-2 tripwire       → tests/lockedVaultDegradation.test.ts (rule 8 gate)
import "fake-indexeddb/auto";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import type { User } from "../src/types";
import type { WrappedVault } from "../src/lib/vault";

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
  listEnvelopes: vi.fn(),
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
vi.mock("../src/api/vault", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/vault")>()),
  listEnvelopes: m.listEnvelopes,
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
  updateTransaction: m.updateTransaction,
  deleteTransaction: m.deleteTransaction,
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

import { AuthProvider } from "../src/auth/AuthContext";
import { AppShell } from "../src/components/layout/AppShell";
import { Transactions } from "../src/pages/Transactions";
import { clearVault, createVault, isUnlocked, lockVault, unlockVault } from "../src/lib/vault";
import { encryptTransaction } from "../src/lib/e2ee";
import { encodeBundle, getDeviceId } from "../src/lib/vaultSync";

// jsdom has no matchMedia; framer-motion and the shell's breakpoint ask for it.
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: /min-width/.test(query),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

const PASSPHRASE = "correct-horse-battery";
const USER_ID = "u1";

const plainUser = (encryptionRequired: boolean): User => ({
  id: USER_ID,
  email: encryptionRequired ? "enrolled@example.com" : "plain@example.com",
  name: null,
  privacy_mode: false,
  currency_tracker_enabled: true,
  llm_provider: "none",
  llm_base_url: null,
  llm_model: "",
  encryption_required: encryptionRequired,
});

/** The plaintext row every tenant has — plaintext is mandatory (docs/33). */
const PLAIN_TXN = {
  id: 902,
  account_id: 7,
  date: "2026-01-06",
  posting_date: null,
  raw_description: "CORNER STORE",
  normalized_payee: "CORNER STORE",
  amount_native: "-12.00",
  currency: "CAD",
  cad_amount: "-12.00",
  category_id: null,
  transfer_pair_id: null,
  pair_account_name: null,
  tags: [],
};

/**
 * One row carrying a real cipher bundle — the enrolled fixture's shape. Built
 * once under a DEK the unlocked cells can unlock, so the ciphertext in the
 * fixture is the ciphertext the app would really serve.
 */
let CIPHER_TXN: Record<string, unknown>;
let vaultBundle: WrappedVault;

beforeAll(async () => {
  vaultBundle = await createVault(PASSPHRASE);
  const ciphers = await encryptTransaction({
    normalizedPayee: "SECRET GROCERY RUN",
    rawDescription: "SECRET GROCERY RUN",
    amountNative: "-42.10",
    cadAmount: "-42.10",
  });
  CIPHER_TXN = {
    ...PLAIN_TXN,
    id: 901,
    date: "2026-01-05",
    raw_description: "SECRET GROCERY RUN",
    normalized_payee: "SECRET GROCERY RUN",
    amount_native: "-42.10",
    cad_amount: "-42.10",
    ...ciphers,
  };
  lockVault();
});

afterAll(() => {
  lockVault();
});

const page = <T,>(items: T[], total = items.length) => ({ items, total, limit: 200, offset: 0 });

/**
 * Enroll THIS browser against the account, so the inline unlock has a real
 * envelope to unlock from (the same shape a device that enrolled elsewhere and
 * then re-opened the app gets). Done through the server's own answer rather
 * than a local IndexedDB mirror so the test exercises the Phase-4 flow the
 * app actually runs.
 */
function enrollThisDevice() {
  m.listEnvelopes.mockResolvedValue({
    items: [
      {
        device_id: getDeviceId(),
        label: "vitest",
        wrapped_blob: encodeBundle(vaultBundle),
        created_at: "2026-10-11T00:00:00.000Z",
        revoked_at: null,
      },
    ],
    revoked_own_device: false,
  });
}

/**
 * The tenant under test, in the one shape that matters to the matrix: does it
 * hold cipher rows at all? `encryption_required` is what makes the server hold
 * them, so a NOT-enrolled tenant can never report a withheld row — which is
 * exactly why rule 7a must keep the lock copy off its surfaces.
 */
let tenantHasCipherRows = false;

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

/** The 2×2: enrollment (server axis) × lock (client axis). */
async function setUp(opts: { encryptionRequired: boolean; unlocked: boolean }) {
  tenantHasCipherRows = opts.encryptionRequired;
  vi.clearAllMocks();
  // No browser-local bundle by default: an enrolled tenant proves enrollment
  // through `users.encryption_required` alone (the fixture tenant has no
  // envelope anywhere, M136 §0-a). The remedy test below stores one on purpose.
  await clearVault(USER_ID);
  if (opts.unlocked) {
    await unlockVault(PASSPHRASE, vaultBundle);
  } else {
    lockVault();
  }
  const user = plainUser(opts.encryptionRequired);
  m.restoreSession.mockResolvedValue({
    access_token: "token",
    refresh_token: "unused",
    token_type: "bearer",
    user,
  });
  m.logout.mockResolvedValue(undefined);
  m.listAccounts.mockResolvedValue(page([]));
  m.listCategories.mockResolvedValue(page([]));
  m.listTags.mockResolvedValue(page([]));
  m.getUnreviewedCount.mockResolvedValue(0);
  m.updateTransaction.mockResolvedValue(PLAIN_TXN);
  m.deleteTransaction.mockResolvedValue(undefined);
  m.setTransactionTags.mockResolvedValue({ items: [] });
  m.getTransactionTags.mockResolvedValue({ items: [] });
  m.fetchAllTransactions.mockResolvedValue(
    tenantHasCipherRows ? [CIPHER_TXN, PLAIN_TXN] : [PLAIN_TXN],
  );
  m.listEnvelopes.mockResolvedValue({ items: [], revoked_own_device: false });

  // A locked search asks the server to hide what it cannot display; the server
  // answers `withheld` with the MEASURED number of rows it dropped. A tenant
  // with no cipher rows has nothing to drop, so `withheld` stays 0 and the
  // lock copy must stay off (docs/33 rule 7a).
  // The ONE row that matches "grocery" is the cipher row. A locked client asks
  // the server to withhold it (the M137 P0-1 flip), so the answer is empty plus
  // a MEASURED `withheld`; an unlocked client gets the row back; a tenant with
  // no cipher rows has neither, which is why rule 7a must keep the lock copy
  // off its surfaces.
  m.searchGlobal.mockImplementation(
    async (_q: string, _token?: string, hideEncrypted = false) => ({
      query: "grocery",
      transactions: tenantHasCipherRows && !hideEncrypted ? [CIPHER_TXN] : [],
      withheld: hideEncrypted && tenantHasCipherRows ? 1 : 0,
      categories: [],
      accounts: [],
      securities: [],
      tags: [],
    }),
  );
  m.listTransactions.mockImplementation(
    async (_filters: unknown, _paging: unknown, opts: { hideEncrypted?: boolean } = {}) => {
      const hide = opts.hideEncrypted === true;
      return {
        ...page(tenantHasCipherRows && !hide ? [CIPHER_TXN] : []),
        withheld: hide && tenantHasCipherRows ? 1 : 0,
      };
    },
  );
  return { user };
}

afterEach(() => {
  lockVault();
});

const CELLS = [
  { encryptionRequired: false, unlocked: false },
  { encryptionRequired: false, unlocked: true },
  { encryptionRequired: true, unlocked: false },
  { encryptionRequired: true, unlocked: true },
] as const;

const label = (c: (typeof CELLS)[number]) =>
  `encryption_required=${c.encryptionRequired}, vault=${c.unlocked ? "unlocked" : "locked"}`;

describe("§3.4-1 degraded-mode matrix — the shell's status chip (rule 7a)", () => {
  for (const cell of CELLS) {
    it(`states the truth for ${label(cell)}`, async () => {
      await setUp(cell);
      mount(<AppShell />);
      // M185: the rail is retired — the TopBar is the desktop chrome signal.
      await waitFor(() => expect(screen.getByTestId("app-topbar")).toBeTruthy());

      if (cell.encryptionRequired && !cell.unlocked) {
        // Enrolled + locked: the ONE cell where claiming "Vault locked" is true.
        const chip = await screen.findByTestId("vault-locked-chip");
        expect(chip.textContent).toContain("Vault locked");
      } else {
        // Not enrolled → the claim would be false (rule 7a).
        // Enrolled but open → an unlocked shell carries no lock copy at all.
        await waitFor(() => expect(screen.queryByTestId("vault-locked-chip")).toBeNull());
      }
    });
  }
});

describe("§3.4-1 degraded-mode matrix — the global search panel (rules 7e/7f)", () => {
  for (const cell of CELLS) {
    it(`states the truth for ${label(cell)}`, async () => {
      await setUp(cell);
      mount(<AppShell />);
      fireEvent.click(await screen.findByTestId("command-palette-trigger"));
      const field = await screen.findByTestId("command-palette-input");
      fireEvent.change(field, { target: { value: "grocery" } });

      await waitFor(() => expect(m.searchGlobal).toHaveBeenCalled());
      // The request itself is part of the contract: ONLY a locked vault asks
      // the server to withhold (the M137 P0-1 flip), and it asks whether or
      // not the tenant has anything to withhold.
      expect(m.searchGlobal.mock.calls[0][2]).toBe(!cell.unlocked);

      const panel = await screen.findByTestId("command-palette");
      await waitFor(() => expect(panel.textContent).not.toContain("Searching…"));

      const withheldSomething = cell.encryptionRequired && !cell.unlocked;
      if (withheldSomething) {
        // A MEASURED withholding replaces the empty state — never stacks (7f).
        const lockNotice = await screen.findByTestId("vault-locked");
        expect(lockNotice.textContent).toContain("Unlock the vault");
        expect(screen.queryByTestId("global-search-empty")).toBeNull();
      } else {
        await waitFor(() => expect(screen.queryByTestId("vault-locked")).toBeNull());
        if (cell.encryptionRequired) {
          // The remedy's outcome: unlocked, the withheld match is back and
          // renders as its DECRYPTED payee — the cipher never reaches the DOM.
          expect(await screen.findByText("SECRET GROCERY RUN")).toBeTruthy();
        } else {
          expect(await screen.findByTestId("command-palette-empty")).toBeTruthy();
        }
      }
      // The palette input itself remains available in every degraded state.
      expect(screen.getByTestId("command-palette-input")).toBeTruthy();
    });
  }
});

describe("§3.4-1 degraded-mode matrix — the S5 search view (A18, rules 7c/7e/7f)", () => {
  for (const cell of CELLS) {
    it(`states the truth for ${label(cell)}`, async () => {
      await setUp(cell);
      mount(<Transactions />);
      await waitFor(() => expect(screen.getByText(/Similar to “grocery”/)).toBeTruthy());

      if (cell.encryptionRequired && !cell.unlocked) {
        const notice = await screen.findByTestId("vault-locked");
        expect(notice.getAttribute("data-vault-variant")).toBe("partial");
        // MEASURED, never invented: one cipher row withheld out of one.
        expect(notice.textContent).toContain("Showing 0 of 1 rows");
        expect(notice.textContent).toContain("Settings → Privacy & data");
        // Rule 7f: the notice REPLACES the empty state — no dead-end "Clear all".
        expect(screen.queryByText("No transactions match these filters.")).toBeNull();
        // Rule 7a/7e: the withheld payee never renders as plaintext.
        expect(screen.queryByText(/SECRET GROCERY RUN/)).toBeNull();
      } else if (cell.encryptionRequired) {
        // THE REMEDY'S OUTCOME (rule 7d): unlocked, the withheld row is back.
        await waitFor(() => expect(screen.queryByTestId("vault-locked")).toBeNull());
        expect(screen.getAllByText(/SECRET GROCERY RUN/).length).toBeGreaterThan(0);
        expect(screen.queryByText("No transactions match these filters.")).toBeNull();
      } else {
        // Not enrolled: a real "no match", said plainly — a lock caveat here
        // would be a false claim about a surface the lock cannot affect (7a).
        await waitFor(() => expect(screen.queryByTestId("vault-locked")).toBeNull());
        expect(
          await screen.findByText("No transactions match these filters."),
        ).toBeTruthy();
      }
    });
  }
});

describe("§3.4-5 remedy exercise — the inline unlock actually restores the feature", () => {
  it("S5 search view: the withheld row comes back after unlocking in place", async () => {
    await setUp({ encryptionRequired: true, unlocked: false });
    // The fixture tenant normally has NO envelope; the remedy needs a bundle
    // this browser can unlock, which is what enrolling this device gives you.
    enrollThisDevice();
    mount(<Transactions />);
    await waitFor(() => expect(screen.getByText(/Similar to “grocery”/)).toBeTruthy());

    const notice = await screen.findByTestId("vault-locked");
    expect(notice.textContent).toContain("Showing 0 of 1 rows");
    expect(screen.queryByText(/SECRET GROCERY RUN/)).toBeNull();

    // ONE interaction, zero navigation (docs/33 rule 7c).
    fireEvent.click(screen.getByTestId("vault-unlock-toggle"));
    const field = await screen.findByLabelText("Encryption passphrase");
    fireEvent.change(field, { target: { value: PASSPHRASE } });
    await act(async () => {
      fireEvent.submit(field.closest("form")!);
    });

    // The affordance must change the OUTCOME, not just the lock state.
    await waitFor(() => expect(isUnlocked()).toBe(true));
    await waitFor(() => expect(screen.queryByTestId("vault-locked")).toBeNull());
    expect(screen.getAllByText(/SECRET GROCERY RUN/).length).toBeGreaterThan(0);
    expect(screen.queryByTestId("vault-unlock-error")).toBeNull();
  });

  it("a WRONG passphrase keeps it locked and says so — the remedy is honest", async () => {
    await setUp({ encryptionRequired: true, unlocked: false });
    enrollThisDevice();
    mount(<Transactions />);
    await screen.findByTestId("vault-locked");

    fireEvent.click(screen.getByTestId("vault-unlock-toggle"));
    const field = await screen.findByLabelText("Encryption passphrase");
    fireEvent.change(field, { target: { value: "not-the-passphrase" } });
    await act(async () => {
      fireEvent.submit(field.closest("form")!);
    });

    expect(await screen.findByTestId("vault-unlock-error")).toBeTruthy();
    expect(screen.getByTestId("vault-locked")).toBeTruthy();
    expect(screen.queryByText(/SECRET GROCERY RUN/)).toBeNull();
  });

  it("global search: the panel's own unlock affordance clears the hint", async () => {
    await setUp({ encryptionRequired: true, unlocked: false });
    enrollThisDevice();
    mount(<AppShell />);
    fireEvent.click(await screen.findByTestId("command-palette-trigger"));
    const field = await screen.findByTestId("command-palette-input");
    fireEvent.change(field, { target: { value: "grocery" } });
    await screen.findByTestId("vault-locked");

    fireEvent.click(screen.getByTestId("vault-unlock-toggle"));
    const passField = await screen.findByLabelText("Encryption passphrase");
    fireEvent.change(passField, { target: { value: PASSPHRASE } });
    await act(async () => {
      fireEvent.submit(passField.closest("form")!);
    });

    // `vaultUnlocked` is part of the query KEY, so the unlock re-runs the
    // search rather than serving the locked answer (docs/33 rule 7c).
    await waitFor(() => expect(screen.queryByTestId("vault-locked")).toBeNull());
    await waitFor(() => expect(screen.getAllByText("SECRET GROCERY RUN").length).toBeGreaterThan(0));
  });
});

describe("§3.4-1 degraded-mode matrix — an UNAFFECTED sub-surface carries no lock copy", () => {
  it("a plaintext metadata match still renders while the vault is locked (rules 7a/7e)", async () => {
    await setUp({ encryptionRequired: true, unlocked: false });
    // Categories are plaintext-by-design, so the lock withholds NOTHING from
    // that group even while it withholds the payee group.
    m.searchGlobal.mockImplementation(async () => ({
      query: "grocery",
      transactions: [],
      withheld: 1,
      categories: [{ id: 1, name: "Groceries" }],
      accounts: [],
      securities: [],
      tags: [],
    }));
    mount(<AppShell />);
    fireEvent.click(await screen.findByTestId("command-palette-trigger"));
    const field = await screen.findByTestId("command-palette-input");
    fireEvent.change(field, { target: { value: "grocery" } });

    expect(await screen.findByText("Groceries")).toBeTruthy();
    // Rule 7a: a caveat on a surface the lock does not affect is a false
    // claim, so the panel — which still answered — carries no lock copy.
    expect(screen.queryByTestId("vault-locked")).toBeNull();
    expect(screen.getByTestId("command-palette-input")).toBeTruthy();
  });
});

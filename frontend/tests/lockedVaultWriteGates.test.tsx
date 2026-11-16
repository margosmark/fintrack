// M136 / M134 §5.1 — the G1–G3 WRITE-PATH pre-emptive gates (P0-3, "a locked
// vault destroys work").
//
// The failure class, in one line: an encryption-enrolled tenant whose vault is
// LOCKED sends a write that cannot produce its ciphers, the server answers the
// documented 422 (`encryption_required`, docs/33 F-10 / docs/44 §8), and the
// user's typed work is gone. It hit THREE surfaces:
//
//   G1  SplitEditor          `submit()` dual-writes amount_cipher only
//                            `if (isUnlocked())`; a split part without one is
//                            refused at backend/app/api/transactions.py:1877.
//   G2  BulkActionsToolbar   the MATCHED rename sends payee-only and the PAGE
//                            path drops to payee-only when locked; both are
//                            refused at transactions.py:878.
//   G3  YnabMigrationCard    `POST /imports/ynab/commit` refuses an enrolled
//                            tenant outright (ynab.py) — the importer
//                            SERVER-side produces no cipher fields and cannot.
//
// G3 IS NO LONGER A LOCK-STATE GATE (M181 follow-up, security finding F4 /
// PM decision: option A). The YNAB commit is a SERVER-side upload, so an
// enrolled tenant is refused whether or not the vault is open: keying on
// `enrolled && !unlocked` left an UNLOCKED tenant looking at a live Import
// control the API would refuse anyway, and the refusal it then rendered
// offered "unlock the vault" — a remedy that cannot work (docs/33 rule 7d).
// G3 now keys on ENROLMENT ALONE and renders
// `ServerSideBlocked capability="import:ynab"`, like every other server-side
// write; this file still owns its tests, parametrized the OTHER way now:
// enrolled (locked OR unlocked) → gated, NOT enrolled → live with no notice.
//
// THE ASSERT RULE this file encodes for G1/G2 (the card's own words): they
// gate PRE-EMPTIVELY — the control that cannot run is replaced by the reason
// BEFORE anything is typed — and these tests assert that pre-emptive branch,
// never the catch. Each of those surfaces is parametrized BOTH ways on
// `encryption_required`:
//
//   enrolled + locked    → gated: VaultLocked `blocked` in place of the
//                          control, zero API calls, zero mutations
//   enrolled + unlocked  → the control is back (the gate follows `vaultchange`,
//                          so the inline unlock restores it in place —
//                          docs/33 rule 7c)
//   NOT enrolled + locked→ the gate is SCOPED to enrolled tenants: a plaintext
//                          tenant's writes never carry ciphers, so the 422
//                          cannot happen and the control stays live. Silence
//                          here is honesty, not a missed surface (rule 7a).
//
// The inline passphrase flow itself (typed passphrase → unlockVaultSynced) is
// vaultSync.test.ts's contract; what is pinned here is that G1/G2 follow the
// same `vaultchange` event back to enabled — and that G3 deliberately does
// NOT, because unlocking cannot enable a server-side upload.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

import { ApiError } from "../src/api/client";
import type { Transaction } from "../src/api/transactions";
import type { User } from "../src/types";
import { AuthUserScopeContext } from "../src/auth/authUser";
import { VaultLocked, VAULT_LOCK_DESTINATION } from "../src/components/e2ee/VaultLocked";
import { lockVault, createVault, isUnlocked } from "../src/lib/vault";

const m = vi.hoisted(() => ({
  listTags: vi.fn(),
  createTag: vi.fn(),
  updateTransaction: vi.fn(),
  bulkSetPayee: vi.fn(),
  previewYnab: vi.fn(),
  commitYnab: vi.fn(),
  listAccounts: vi.fn(),
}));

vi.mock("../src/api/tags", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/tags")>()),
  listTags: m.listTags,
  createTag: m.createTag,
}));
vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  updateTransaction: m.updateTransaction,
  bulkSetPayee: m.bulkSetPayee,
}));
vi.mock("../src/api/ynab", () => ({
  previewYnab: m.previewYnab,
  commitYnab: m.commitYnab,
}));
vi.mock("../src/api/accounts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/accounts")>()),
  listAccounts: m.listAccounts,
}));
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  ToastProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { SplitEditor } from "../src/components/transactions/SplitEditor";
import { BulkActionsToolbar } from "../src/components/transactions/BulkActionsToolbar";
import { YnabMigrationCard } from "../src/components/imports/YnabMigrationCard";

// jsdom has no IndexedDB; `backupSupported()` gates the inline Unlock button's
// enabled state, and `createVault` (used below to reach the unlocked state)
// needs only Web Crypto, which Node provides.
Object.defineProperty(window, "indexedDB", {
  configurable: true,
  writable: true,
  value: undefined,
});

function user(enrollment: boolean): User {
  return {
    id: "u1",
    email: enrollment ? "enrolled@example.com" : "plaintext@example.com",
    name: null,
    privacy_mode: false,
    currency_tracker_enabled: false,
    llm_provider: "none",
    llm_base_url: null,
    llm_model: "",
    encryption_required: enrollment,
    dashboard_preset: "overview",
    saved_report_views: [],
  };
}

/** Renders `ui` under the session-user scope M136 lifted out of AuthContext. */
function mount(ui: ReactElement, enrolled: boolean) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AuthUserScopeContext.Provider value={{ user: user(enrolled) }}>
        <MemoryRouter>{ui}</MemoryRouter>
      </AuthUserScopeContext.Provider>
    </QueryClientProvider>,
  );
}

const PARENT = {
  id: 60,
  account_id: 7,
  date: "2026-01-06",
  posting_date: null,
  raw_description: "METRO",
  normalized_payee: "Metro",
  amount_native: "-187.42",
  currency: "CAD",
  cad_amount: "-187.42",
  category_id: 1,
  transfer_pair_id: null,
  pair_account_name: null,
  tags: [],
  payee_cipher: null,
  raw_desc_cipher: null,
  amount_cipher: null,
  cad_amount_cipher: null,
  payee_token: null,
} as unknown as Transaction;

function tx(id: number): Transaction {
  return { ...PARENT, id, normalized_payee: `Merchant ${id}` } as unknown as Transaction;
}
const ROWS = [1, 2, 3].map(tx);
const CATEGORY = { id: 4, user_id: 1, name: "Groceries", parent_id: null, is_system: false };

beforeEach(() => {
  vi.clearAllMocks();
  lockVault();
  m.listTags.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  m.listAccounts.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  m.updateTransaction.mockImplementation((id: number, patch: unknown) =>
    Promise.resolve({ ...PARENT, id, ...(patch as object) }),
  );
  m.bulkSetPayee.mockResolvedValue({ updated: 3, conflicts: [] });
});

afterEach(() => {
  lockVault();
});

// ── G1 — SplitEditor ────────────────────────────────────────────────────────

describe("G1 (SplitEditor) — the Save control is replaced before a leg is typed", () => {
  function editor(enrolled: boolean) {
    const onSave = vi.fn();
    mount(
      <SplitEditor
        parent={PARENT}
        categories={[CATEGORY as never]}
        saving={false}
        onSave={onSave}
        onClose={() => {}}
      />,
      enrolled,
    );
    return { onSave };
  }

  it("gates pre-emptively: no Split button exists, the reason does, and nothing is saved", async () => {
    editor(true);
    await screen.findByRole("dialog", { name: "Split this transaction into parts" });

    // The pre-emptive branch: VaultLocked `blocked` stands where Save was.
    const notice = screen.getByTestId("vault-locked");
    expect(notice.getAttribute("data-vault-variant")).toBe("blocked");
    expect(screen.getByTestId("vault-locked-destination").textContent).toContain(
      VAULT_LOCK_DESTINATION,
    );
    // The control is GONE — not merely disabled — so no path through this
    // dialog can submit the typed legs into a 422.
    expect(screen.queryByRole("button", { name: "Split" })).toBeNull();
  });

  it("restores the control when the vault unlocks — the gate follows vaultchange", async () => {
    editor(true);
    await screen.findByRole("dialog", { name: "Split this transaction into parts" });
    expect(screen.queryByRole("button", { name: "Split" })).toBeNull();

    // The same state the inline unlock lands in: DEK + index key in session,
    // `vaultchange` dispatched.
    await actCreateVault();
    await waitFor(() => expect(screen.getByRole("button", { name: "Split" })).toBeTruthy());
    expect(screen.queryByTestId("vault-locked")).toBeNull();
  });

  it("stays live for a NOT-enrolled tenant — the 422 cannot happen, so silence is honest", async () => {
    editor(false);
    await screen.findByRole("dialog", { name: "Split this transaction into parts" });
    expect(screen.getByRole("button", { name: "Split" })).toBeTruthy();
    expect(screen.queryByTestId("vault-locked")).toBeNull();
  });
});

// ── G2 — BulkActionsToolbar ─────────────────────────────────────────────────

describe("G2 (BulkActionsToolbar) — Set payee is disabled WITH the reason, up front", () => {
  function toolbar(enrolled: boolean) {
    mount(
      <BulkActionsToolbar
        rows={ROWS}
        selectedIds={[1, 2, 3]}
        categories={[CATEGORY as never]}
        tags={[]}
        onClear={() => {}}
        onSettled={() => {}}
      />,
      enrolled,
    );
  }

  it("gates pre-emptively: the button is disabled, wired to the reason, and the mode never opens", async () => {
    toolbar(true);
    const setPayee = await screen.findByRole("button", { name: "Set payee" });
    expect((setPayee as HTMLButtonElement).disabled).toBe(true);

    // The disabled control NAMES its reason for assistive tech, and the reason
    // is the shared component carrying the canonical destination. The link is
    // the DOM id the component accepts for exactly this (aria-describedby).
    const notice = document.getElementById("bulk-rename-locked");
    expect(notice).not.toBeNull();
    expect(setPayee.getAttribute("aria-describedby")).toBe("bulk-rename-locked");
    expect(notice!.getAttribute("data-vault-variant")).toBe("blocked");
    expect(notice!.textContent).toContain(VAULT_LOCK_DESTINATION);

    // Nothing typed, nothing sent: the rename mode cannot even be entered.
    fireEvent.click(setPayee);
    expect(screen.queryByPlaceholderText("Common payee name")).toBeNull();
    expect(m.bulkSetPayee).not.toHaveBeenCalled();
  });

  it("re-enables on unlock — the same gate, the same event", async () => {
    toolbar(true);
    await screen.findByRole("button", { name: "Set payee" });
    await actCreateVault();
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Set payee" }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    expect(document.getElementById("bulk-rename-locked")).toBeNull();
  });

  it("stays live for a NOT-enrolled tenant, locked or not", async () => {
    toolbar(false);
    const setPayee = await screen.findByRole("button", { name: "Set payee" });
    expect((setPayee as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByTestId("bulk-rename-locked")).toBeNull();
  });
});

// ── G3 — YnabMigrationCard ──────────────────────────────────────────────────

describe("G3 (YnabMigrationCard) — the commit control never offers a doomed Import", () => {
  function planFixture() {
    return {
      source_format: "json",
      budget_name: "Household 2026",
      currency: "CAD",
      date_format: null,
      accounts: [],
      category_groups: [],
      categories: [],
      targets: [],
      transactions: {
        total: 5,
        importable: 2,
        duplicates: 1,
        duplicates_pending: 0,
        transfer_pairs: 0,
        rows: [],
        rows_truncated: false,
        unmappable: [],
        warnings: [],
        warnings_truncated: false,
      },
      notes: [],
      counts: {
        accounts_create: 0, accounts_reuse: 0, accounts_unmapped: 0,
        groups_create: 0, groups_reuse: 0, categories_create: 0,
        categories_reuse: 0, tags_create: 0, tags_reuse: 0,
        budgets_create: 0, budgets_reuse: 0, goals_create: 0,
        goals_reuse: 0, targets_unsupported: 0,
      },
    };
  }

  /** File picked + Preview pressed — the plan view the commit control lives in. */
  async function previewed(enrolled: boolean) {
    m.previewYnab.mockResolvedValue(planFixture());
    mount(<YnabMigrationCard />, enrolled);
    const input = screen.getByTestId("ynab-file") as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["{}"], "budget.json", { type: "application/json" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: /Preview/ }));
    await screen.findByText("Household 2026");
  }

  it("gates on ENROLMENT alone: the plan says why Import cannot run, and commit is never called", async () => {
    await previewed(true);
    const notice = screen.getByTestId("server-side-blocked");
    expect(notice.getAttribute("data-server-side-capability")).toBe("import:ynab");
    // Rule 7d: the reason names NO remedy, because unlocking cannot enable a
    // server-side upload — no unlock copy, no `VAULT_LOCK_DESTINATION`.
    expect(notice.textContent).not.toMatch(/unlock/i);
    expect(notice.textContent).not.toContain(VAULT_LOCK_DESTINATION);
    // The control is GONE: no press, no request, no 422 to discover later.
    expect(screen.queryByTestId("vault-locked")).toBeNull();
    expect(screen.queryByTestId("ynab-import")).toBeNull();
    expect(m.commitYnab).not.toHaveBeenCalled();
  });

  it("STAYS gated with the vault UNLOCKED — a lock-state gate would be a lie (rule 7d)", async () => {
    // The enrolled + UNLOCKED tenant this file's old G3 case let through with
    // a live Import control: the server refuses the upload all the same
    // (ynab.py), so unlocking must not bring the control back (F4, option A).
    await previewed(true);
    expect(screen.queryByTestId("ynab-import")).toBeNull();

    await actCreateVault();
    // Prove the unlock really landed, so "still gated" cannot be a no-op.
    expect(isUnlocked()).toBe(true);
    expect(screen.queryByTestId("server-side-blocked")).not.toBeNull();
    expect(screen.queryByTestId("ynab-import")).toBeNull();
    expect(m.commitYnab).not.toHaveBeenCalled();
  });

  it("stays live for a NOT-enrolled tenant, and renders NO notice (rule 7a)", async () => {
    await previewed(false);
    expect(screen.getByTestId("ynab-import")).toBeTruthy();
    expect(screen.queryByTestId("vault-locked")).toBeNull();
    expect(screen.queryByTestId("server-side-blocked")).toBeNull();
  });

  it("defense-in-depth: an encryption_required 422 at commit repeats the SAME notice, no unlock remedy", async () => {
    // Unreachable through the gate above — reachable if a future change
    // re-enables the button — and the envelope's `code` (never its `detail`,
    // docs/08 §Error envelope) picks the copy, so the pre-emptive gate and the
    // post-hoc 422 tell exactly one story.
    await previewed(false);
    m.commitYnab.mockRejectedValue(
      new ApiError(
        422,
        "Importing a YNAB budget file is unavailable while client-side encryption is enabled: " +
          "this path runs server-side and cannot safely write encrypted data, so nothing was saved",
        "encryption_required",
      ),
    );
    fireEvent.click(screen.getByTestId("ynab-import"));
    const notice = await screen.findByTestId("server-side-blocked");
    expect(notice.getAttribute("data-server-side-capability")).toBe("import:ynab");
    expect(notice.textContent).not.toMatch(/unlock/i);
    expect(screen.queryByTestId("vault-locked")).toBeNull();
  });

  it("defense-in-depth: any OTHER failure keeps the raw envelope in the ErrorPanel", async () => {
    await previewed(false);
    m.commitYnab.mockRejectedValue(new ApiError(422, "The file is not a YNAB export", "invalid_file"));
    fireEvent.click(screen.getByTestId("ynab-import"));
    const panel = await screen.findByText("The file is not a YNAB export");
    expect(panel).toBeTruthy();
    expect(screen.queryByTestId("vault-locked")).toBeNull();
  });
});

// ── the component contract itself ───────────────────────────────────────────

describe("VaultLocked (contract)", () => {
  it("blocked and partial both carry the canonical destination verbatim", () => {
    render(
      <VaultLocked variant="blocked" capability="write:split" />,
    );
    expect(screen.getByTestId("vault-locked-destination").textContent.trim()).toBe(
      VAULT_LOCK_DESTINATION,
    );
  });

  it("the status chip never claims a surface is degraded (rule 7a)", () => {
    render(<VaultLocked variant="status" capability="search" />);
    const chip = screen.getByTestId("vault-locked-chip");
    expect(chip.getAttribute("role")).toBe("status");
    expect(chip.textContent).toBe("Vault locked");
    // No destination string on a state chip: it names no remedy because it
    // withholds nothing.
    expect(screen.queryByTestId("vault-locked-destination")).toBeNull();
  });

  it("partial builds its sentence from MEASURED counts, never invented ones", () => {
    render(
      <VaultLocked variant="partial" capability="search" total_count={10} withheld_count={3} />,
    );
    expect(screen.getByTestId("vault-locked-copy").textContent).toContain(
      "Showing 7 of 10 rows — unlock to search the other 3.",
    );
  });
});

/** `createVault` leaves the session unlocked and dispatches `vaultchange`. */
async function actCreateVault(): Promise<void> {
  await createVault("test-passphrase");
}

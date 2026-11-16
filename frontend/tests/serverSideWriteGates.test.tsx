// M181 follow-up (docs/33 §Server-first computing rules 6 / 7a / 7b / 7d) —
// the SERVER-SIDE ingestion write gates.
//
// The failure class, in one line: an `encryption_required` tenant reaches a
// control whose write the SERVER must perform, does the work, and only
// discovers the refusal in a 422 `encryption_required` after the fact — which
// is exactly what rule 6 forbids ("DISABLED WITH REASON before submit — never
// a post-hoc 422 on a save the user already performed").
//
// What makes this family DIFFERENT from `lockedVaultWriteGates.test.tsx`
// (G1–G2), and what this file pins hardest:
//
//   ENROLMENT ALONE. G1–G2 gate on `encryption_required && !unlocked`, because
//   the browser CAN produce the missing ciphers once the vault is open. These
//   surfaces cannot: `trades` has no cipher columns at all and the statement
//   row is written by the importer, so unlocking changes nothing. The gate
//   therefore keys on `users.encryption_required` alone — a lock-state gate
//   here would both LIE (the remedy does not work — rule 7d) and hide the
//   reason from an unlocked-but-enrolled user.
//
//   G3 (`YnabMigrationCard`, capability `import:ynab`) JOINED this family on
//   the M181 follow-up (security finding F4, PM option A): the YNAB commit is
//   a server-side upload too, so it keys on enrolment alone. Its cases stay in
//   `lockedVaultWriteGates.test.tsx`, which owns the card's harness; the copy
//   contract below covers its string like every other one here.
//
// Three parametrizations per surface, the same cut as G1–G2:
//
//   enrolled (+ locked or UNLOCKED) → gated: `server-side-blocked` in place of
//                                     the control, zero write calls
//   NOT enrolled                   → the control is live and NO notice renders
//                                     at all (rule 7a — silence is honesty)
//
// Plus the copy contract, checked on the notice itself: it names the
// capability, it says why, and it offers NO remedy — no "Unlock the vault",
// no `VAULT_LOCK_DESTINATION` (rule 7d bans a remedy that cannot restore the
// feature).
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../src/auth/AuthContext";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { VAULT_LOCK_DESTINATION } from "../src/components/e2ee/VaultLocked";
import { createVault, lockVault } from "../src/lib/vault";

const h = vi.hoisted(() => {
  const user = (enrolled: boolean) => ({
    id: "user-1",
    email: enrolled ? "enrolled@example.com" : "plain@example.com",
    privacy_mode: false,
    currency_tracker_enabled: false,
    llm_provider: "openai",
    llm_base_url: null,
    llm_model: "gpt-4o-mini",
    encryption_required: enrolled,
  });
  // A REAL investment type: `DividendSuggestionsSection` and the import-mode
  // derivation both read INVESTMENT_ACCOUNT_TYPES membership, and the older
  // harnesses' `brokerage_rrsp` is not in that set.
  const investment = {
    id: 1,
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
  };
  const chequing = {
    ...investment,
    id: 7,
    name: "Everyday chequing",
    type: "chequing",
    institution_id: 3,
    investment_value: "0.00",
  };
  const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 200, offset: 0 });
  const position = {
    account_id: 1,
    account_name: "TFSA",
    security_id: 7,
    symbol: "MSFT",
    listing_currency: "USD",
    quantity: "35.2491",
    price_date: "2026-10-08",
    price: "517.42",
    price_source: "yfinance",
    price_age_days: 1,
    price_stale: false,
    book_cost_cad: "17432.11",
    spark: ["500", "505", "510", "517.42"],
    market_value_native: "18238.60",
    market_value_cad: "18239.44",
    fx_rate_to_cad: "1.4",
    previous_close_native: "510.00",
    day_change_native: "7.42",
    day_change_pct: 1.45,
    day_change_cad: "366.17",
    unrealized_gain_cad: "807.33",
    unrealized_gain_native: "590.22",
    unrealized_gain_pct: 4.63,
  };
  return {
    restoreSession: vi.fn(),
    me: vi.fn(),
    // investments / security detail
    listAccounts: vi.fn(),
    getPositions: vi.fn(),
    getPortfolioValueHistory: vi.fn(),
    getWatchlist: vi.fn(),
    listSecurities: vi.fn(),
    listTrades: vi.fn(),
    getDividendSuggestions: vi.fn(),
    getExposure: vi.fn(),
    getBreakdown: vi.fn(),
    getSecurityOverview: vi.fn(),
    getPriceHistory: vi.fn(),
    createTrade: vi.fn(),
    recordDividend: vi.fn(),
    // imports
    listInstitutions: vi.fn(),
    listImports: vi.fn(),
    previewImport: vi.fn(),
    suggestProfile: vi.fn(),
    uploadImport: vi.fn(),
    uploadImportWithProfile: vi.fn(),
    commitBatch: vi.fn(),
    discardBatch: vi.fn(),
    getBatch: vi.fn(),
    getBatchRows: vi.fn(),
    importTrades: vi.fn(),
    commitTradeBatch: vi.fn(),
    user,
    investment,
    chequing,
    page,
    position,
  };
});

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  restoreSession: h.restoreSession,
  me: h.me,
}));
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
      return { accounts, investmentAccounts: actual.filterInvestmentAccounts(accounts.data?.items ?? []) };
    },
  };
});
vi.mock("../src/api/portfolio", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/portfolio")>()),
  getPositions: h.getPositions,
  getPortfolioValueHistory: h.getPortfolioValueHistory,
  getExposure: h.getExposure,
  getBreakdown: h.getBreakdown,
}));
vi.mock("../src/api/watchlist", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/watchlist")>()),
  getWatchlist: h.getWatchlist,
}));
vi.mock("../src/api/securities", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/securities")>()),
  listSecurities: h.listSecurities,
  getSecurityOverview: h.getSecurityOverview,
}));
vi.mock("../src/api/prices", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/prices")>()),
  getPriceHistory: h.getPriceHistory,
}));
vi.mock("../src/api/trades", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/trades")>()),
  listTrades: h.listTrades,
  getDividendSuggestions: h.getDividendSuggestions,
  createTrade: h.createTrade,
  recordDividend: h.recordDividend,
  importTrades: h.importTrades,
  commitTradeBatch: h.commitTradeBatch,
}));
vi.mock("../src/api/imports", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/imports")>()),
  listImports: h.listImports,
  previewImport: h.previewImport,
  suggestProfile: h.suggestProfile,
  uploadImport: h.uploadImport,
  uploadImportWithProfile: h.uploadImportWithProfile,
  commitBatch: h.commitBatch,
  discardBatch: h.discardBatch,
  getBatch: h.getBatch,
  getBatchRows: h.getBatchRows,
}));
vi.mock("../src/api/institutions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/institutions")>()),
  listInstitutions: h.listInstitutions,
}));
// Toast is context-free here: no surface under test reports through it, and a
// real ToastProvider arms auto-dismiss timers the runner would inherit.
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  ToastProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { Investments } from "../src/pages/Investments";
import { SecurityDetail } from "../src/pages/SecurityDetail";
import { Imports } from "../src/pages/Imports";

/** jsdom has no matchMedia (ThemeProvider / framer-motion ask for it). */
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
  onerror: ((e: ErrorEvent) => void) | null = null;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  close(): void {}
}

function wrap(ui: ReactElement, path: string, entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <AuthProvider>
            <Routes>
              <Route path={path} element={ui} />
              <Route path="*" element={ui} />
            </Routes>
          </AuthProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/**
 * The gate notices on screen for ONE capability — the pages carry two gated
 * surfaces (the trade form and the dividend row), so a bare testid query
 * cannot tell them apart.
 */
function noticeFor(capability: string): Element | null {
  return document.querySelector(`[data-server-side-capability="${capability}"]`);
}
function noticeCount(): number {
  return document.querySelectorAll('[data-testid="server-side-blocked"]').length;
}

/** Rule 7d, on the notice itself: no remedy that cannot restore the feature. */
function expectNoRemedy(notice: Element | null): void {
  expect(notice).not.toBeNull();
  const text = notice!.textContent ?? "";
  expect(text).not.toMatch(/unlock/i);
  expect(text).not.toContain(VAULT_LOCK_DESTINATION);
}

const SUGGESTION = {
  security_id: 7,
  account_id: 1,
  symbol: "MSFT",
  date: "2026-10-15",
  amount: "0.83",
  quantity: "35.2491",
  gross_amount: "29.26",
  currency: "USD",
};

const OVERVIEW = {
  security: {
    id: 7,
    symbol: "MSFT",
    name: "Microsoft Corporation",
    kind: "stock",
    listing_currency: "USD",
    exchange: "NMS",
  },
  latest: {
    date: "2026-10-08",
    price: "517.42",
    source: "yfinance",
    age_days: 1,
    stale: false,
    previous_close: "510.00",
    day_change_native: "7.42",
    day_change_pct: 1.45,
  },
  stats: { high_52w: "520.00", low_52w: "380.00", change_pct: 5.2, cached_days: 300 },
  history: [{ date: "2026-10-07", price: "510.00" }],
  positions: [h.position],
  totals: {
    quantity: "35.2491",
    book_cost_cad: "17432.11",
    market_value_cad: "18239.44",
    unrealized_gain_cad: "807.33",
    unrealized_gain_native: "590.22",
    unrealized_gain_pct: 4.63,
    day_change_cad: "366.17",
  },
  watchlisted: false,
  fundamentals: null,
  holdings: null,
  recent_trades: [],
};

const DRY_RUN = {
  format: "csv_profile",
  mapping: {
    format: "csv_profile",
    kind: "csv_profile",
    fields: [
      { field: "date", source: "Date" },
      { field: "raw_description", source: "Description" },
      { field: "amount_native", source: "Amount" },
    ],
    mode: "signed",
    date_format: "%Y-%m-%d",
    decimal_comma: false,
    credits_positive: true,
    headerless: false,
  },
  rows: [
    {
      index: 0,
      date: "2024-01-02",
      amount_native: "-12.50",
      currency: "CAD",
      raw_description: "COFFEE SHOP",
      cad_amount: "-12.50",
      verdict: "new",
      near_duplicate_match: null,
    },
  ],
  summary: { total: 1, new: 1, duplicates: 0, near_duplicates: 0, unresolved: 0 },
};

const BATCH = {
  id: 11,
  account_id: 7,
  institution: "Unprofiled Bank",
  file_name: "statement.csv",
  status: "pending_review",
  imported: 1,
  duplicates_skipped: 0,
  unresolved: 0,
};

const BATCH_ROWS = [
  {
    id: 901,
    date: "2024-01-02",
    amount_native: "-12.50",
    currency: "CAD",
    raw_description: "COFFEE SHOP",
    cad_amount: "-12.50",
    duplicate: false,
    near_duplicate: false,
  },
];

/**
 * `restoreSession`/`me` are read once at mount, so the enrolment flag is set
 * through this cell BEFORE each render rather than by a re-auth.
 */
const currentEnrollment = { enrolled: false };
function asTenant(enrolled: boolean): void {
  currentEnrollment.enrolled = enrolled;
}

beforeEach(() => {
  vi.clearAllMocks();
  lockVault();
  FakeEventSource.instances = [];
  h.restoreSession.mockImplementation((_opts?: unknown) => {
    const enrolled = currentEnrollment.enrolled;
    return Promise.resolve({
      access_token: "token",
      token_type: "bearer",
      user: h.user(enrolled),
    });
  });
  h.me.mockImplementation(() => Promise.resolve(h.user(currentEnrollment.enrolled)));
  h.listAccounts.mockResolvedValue(h.page([h.investment, h.chequing]));
  h.listInstitutions.mockResolvedValue(h.page([{ id: 3, name: "Unprofiled Bank", importer_profile: null }]));
  h.getPositions.mockResolvedValue({ items: [h.position], total: 1 });
  h.getPortfolioValueHistory.mockResolvedValue({ items: [], total: 0 });
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([SUGGESTION]);
  h.getExposure.mockResolvedValue({ items: [], total: 0 });
  h.getBreakdown.mockResolvedValue({ dimension: "sector", items: [], total_cad: "0", unclassified_cad: "0" });
  h.getPriceHistory.mockResolvedValue({ items: [], security: null });
  h.getSecurityOverview.mockResolvedValue(OVERVIEW);
  h.createTrade.mockResolvedValue({});
  h.recordDividend.mockResolvedValue({});
  h.listImports.mockResolvedValue(h.page([]));
  h.previewImport.mockResolvedValue(DRY_RUN);
  h.uploadImport.mockResolvedValue({
    id: 11,
    account_id: 7,
    institution: "Unprofiled Bank",
    file_name: "statement.csv",
    status: "pending_review",
    imported: 1,
    duplicates_skipped: 0,
    unresolved: 0,
    preview: [],
  });
  h.importTrades.mockResolvedValue({
    batch_id: 12,
    imported: 1,
    duplicates_skipped: 0,
    institution: null,
    notices: [],
    preview: [],
    unmapped: [],
  });
  h.commitBatch.mockResolvedValue({ ...BATCH, status: "committed" });
  h.commitTradeBatch.mockResolvedValue({ ...BATCH, id: 12, status: "committed" });
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  lockVault();
  vi.clearAllMocks();
});

async function renderInvestments(enrolled: boolean) {
  asTenant(enrolled);
  wrap(<Investments />, "*", "/investments");
  await screen.findByRole("heading", { name: "Record trade" });
  // Wait for POSITIONS too: `usePriceStream` only opens the SSE connection once
  // they land, and letting that effect fire after the test body ends would race
  // `vi.unstubAllGlobals()` in afterEach (the "EventSource is not defined" red).
  // The Σ row, not the symbol: "MSFT" also appears in the dividend row.
  await screen.findByTestId("positions-total-row");
}

async function renderSecurity(enrolled: boolean) {
  asTenant(enrolled);
  wrap(<SecurityDetail />, "/securities/:securityId", "/securities/7");
  await screen.findByRole("heading", { name: "Record Dividend — MSFT" });
  // Same SSE-timing guard as `renderInvestments`: let the stream effect flush
  // inside the test, not after `vi.unstubAllGlobals()`.
  await screen.findByTestId("impact-card");
}

function renderImports(enrolled: boolean) {
  asTenant(enrolled);
  wrap(<Imports />, "*", "/imports");
}

/** File + account staged in the real upload form (the m112 driver). */
async function stageUpload(accountValue: string) {
  await screen.findByText(/Everyday chequing \(CAD\)/);
  const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
  if (!fileInput) throw new Error("no file input in the upload form");
  fireEvent.change(fileInput, {
    target: {
      files: [new File(["date,description,amount\n1/2/2024,COFFEE,-12.50\n"], "statement.csv", { type: "text/csv" })],
    },
  });
  const form = fileInput.closest("form");
  if (!form) throw new Error("file input is not inside a form");
  const accountSelect = form.querySelector("select");
  if (!accountSelect) throw new Error("no account select in the upload form");
  fireEvent.change(accountSelect, { target: { value: accountValue } });
  return form;
}

// ── G4 — the manual trade form (POST /trades) ───────────────────────────────

describe("G4 (TradeForm) — Record trade is replaced by its reason before a share is typed", () => {
  it("gates on ENROLMENT alone: the control is gone and no write is sent", async () => {
    await renderInvestments(true);

    expect(screen.queryByRole("button", { name: "Record trade" })).toBeNull();
    const notice = noticeFor("write:trade");
    expect(notice?.getAttribute("id")).toBe("trade-form-blocked");
    expect(notice?.getAttribute("role")).toBe("status");
    // Rule 7b: names the capability; the reason is the server-side fact.
    expect(notice?.textContent).toContain("Recording a trade");
    expect(notice?.textContent).toContain("runs server-side");
    expectNoRemedy(notice);

    // Even a forced submit (Enter in a field) goes nowhere.
    const form = document.getElementById("trade-form")?.querySelector("form");
    expect(form).toBeTruthy();
    fireEvent.submit(form!);
    expect(h.createTrade).not.toHaveBeenCalled();
  });

  it("STAYS gated with the vault UNLOCKED — a lock-state gate would be a lie (rule 7d)", async () => {
    await renderInvestments(true);
    expect(noticeFor("write:trade")).not.toBeNull();

    await act(async () => {
      await createVault("test-passphrase");
    });

    // Unlocking cannot make `POST /trades` able to emit ciphers, so the
    // control must NOT come back — the whole point of the enrolment-only rule.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Record trade" })).toBeNull());
    expect(noticeFor("write:trade")).not.toBeNull();
    expect(h.createTrade).not.toHaveBeenCalled();
  });

  it("carries NO lock copy for a NOT-enrolled tenant (rule 7a)", async () => {
    await renderInvestments(false);

    expect(screen.getByRole("button", { name: "Record trade" })).toBeTruthy();
    expect(noticeCount()).toBe(0);
  });
});

// ── G5 — the dividend suggestion row (POST /trades/record-dividend) ─────────

describe("G5 (dividend suggestions) — Record is replaced; Dismiss still works", () => {
  it("gates on ENROLMENT alone, and the removal path stays available", async () => {
    await renderInvestments(true);

    expect(screen.queryByRole("button", { name: "Record" })).toBeNull();
    expectNoRemedy(noticeFor("write:dividend"));
    expect(h.recordDividend).not.toHaveBeenCalled();
    // Dismiss is a removal path (docs/33 exclusion table) — it must survive.
    expect(screen.getByLabelText("Dismiss dividend suggestion")).toBeTruthy();
  });

  it("stays live with the vault unlocked too", async () => {
    await renderInvestments(true);
    await act(async () => {
      await createVault("test-passphrase");
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Record" })).toBeNull());
    expect(noticeFor("write:dividend")).not.toBeNull();
  });

  it("carries NO lock copy for a NOT-enrolled tenant (rule 7a)", async () => {
    await renderInvestments(false);

    expect(screen.getByRole("button", { name: "Record" })).toBeTruthy();
    expect(noticeCount()).toBe(0);
  });
});

// ── G6 — SecurityDetail's Record Dividend card ──────────────────────────────

describe("G6 (RecordDividendCard) — the submit control is replaced before anything is typed", () => {
  it("gates on ENROLMENT alone; a forced submit posts nothing", async () => {
    await renderSecurity(true);

    expect(screen.queryByRole("button", { name: "Record dividend" })).toBeNull();
    const notice = noticeFor("write:dividend");
    expect(notice?.getAttribute("id")).toBe("record-dividend-blocked");
    expect(notice?.textContent).toContain("Recording a dividend");
    expectNoRemedy(notice);

    fireEvent.submit(notice!.closest("form")!);
    expect(h.recordDividend).not.toHaveBeenCalled();
  });

  it("stays gated with the vault unlocked", async () => {
    await renderSecurity(true);
    await act(async () => {
      await createVault("test-passphrase");
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Record dividend" })).toBeNull());
    expect(noticeFor("write:dividend")).not.toBeNull();
  });

  it("carries NO lock copy for a NOT-enrolled tenant (rule 7a)", async () => {
    await renderSecurity(false);

    expect(screen.getByRole("button", { name: "Record dividend" })).toBeTruthy();
    expect(noticeCount()).toBe(0);
  });
});

// ── G7 — the import wizard ──────────────────────────────────────────────────

describe("G7 (import wizard) — writes gated, zero-write previews untouched", () => {
  it("the BANK path keeps its dry run (zero writes) and gates only the Confirm that stages", async () => {
    renderImports(true);
    const form = await stageUpload("7");

    // The preview control is the ZERO-write `POST /imports/preview`: available
    // to an enrolled tenant (docs/33 exclusion table), so it is NOT gated.
    const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]');
    expect(submit).toBeTruthy();
    expect(submit!.textContent).toContain("Preview import");
    fireEvent.click(submit!);

    expect(await screen.findByText("Preview — nothing is saved yet")).toBeTruthy();
    await waitFor(() => expect(h.previewImport).toHaveBeenCalledTimes(1));

    // …and the write that would stage those rows IS gated, pre-emptively.
    expect(screen.queryByRole("button", { name: /Confirm import of 1 row/ })).toBeNull();
    const notice = noticeFor("import:statement");
    expect(notice?.getAttribute("id")).toBe("import-confirm-blocked");
    expect(notice?.textContent).toContain("Importing a statement file");
    expectNoRemedy(notice);
    expect(h.uploadImport).not.toHaveBeenCalled();
    expect(h.uploadImportWithProfile).not.toHaveBeenCalled();
    // Cancel — the other half of the preview pair — stays live.
    expect(screen.getByRole("button", { name: /Cancel — import nothing/ })).toBeTruthy();
  });

  it("the bank path is untouched for a NOT-enrolled tenant (rule 7a)", async () => {
    renderImports(false);
    const form = await stageUpload("7");
    fireEvent.click(form.querySelector<HTMLButtonElement>('button[type="submit"]')!);

    expect(await screen.findByText("Preview — nothing is saved yet")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Confirm import of 1 row/ })).toBeTruthy();
    expect(noticeCount()).toBe(0);
  });

  it("the INVESTMENT upload is gated before the file is parsed (POST /trades/import)", async () => {
    renderImports(true);
    const form = await stageUpload("1");

    expect(screen.queryByRole("button", { name: "Upload & parse" })).toBeNull();
    const notice = noticeFor("import:trades");
    expect(notice?.getAttribute("id")).toBe("import-upload-blocked");
    expect(notice?.textContent).toContain("Importing a brokerage activity file");
    expectNoRemedy(notice);

    // Enter in a field would still submit — the handler refuses too.
    fireEvent.submit(form);
    expect(h.importTrades).not.toHaveBeenCalled();
    expect(h.uploadImport).not.toHaveBeenCalled();
  });

  it("the investment upload is live for a NOT-enrolled tenant (rule 7a)", async () => {
    renderImports(false);
    const form = await stageUpload("1");

    expect(screen.getByRole("button", { name: "Upload & parse" })).toBeTruthy();
    expect(noticeCount()).toBe(0);
    fireEvent.click(form.querySelector<HTMLButtonElement>('button[type="submit"]')!);
    await waitFor(() => expect(h.importTrades).toHaveBeenCalledTimes(1));
  });

  it("a batch staged BEFORE enrolment: Open still works, Commit is gated, Discard is not", async () => {
    // The real shape of the enrol-after-staging case: `discard`/`revert` are
    // the ONLY way to clear such rows, so history stays reachable — but making
    // them live is the `POST /imports/{id}/commit` the guard refuses.
    h.listImports.mockResolvedValue(h.page([BATCH]));
    h.getBatch.mockResolvedValue(BATCH);
    h.getBatchRows.mockResolvedValue(BATCH_ROWS);

    renderImports(true);
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));

    expect(await screen.findByRole("button", { name: /Discard batch/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Commit 1 new transaction/ })).toBeNull();
    const notice = noticeFor("import:statement-commit");
    expect(notice?.getAttribute("id")).toBe("import-commit-blocked");
    expect(notice?.textContent).toContain("Committing a staged import batch");
    expectNoRemedy(notice);
    expect(h.commitBatch).not.toHaveBeenCalled();
  });

  it("the same staged batch commits normally for a NOT-enrolled tenant (rule 7a)", async () => {
    h.listImports.mockResolvedValue(h.page([BATCH]));
    h.getBatch.mockResolvedValue(BATCH);
    h.getBatchRows.mockResolvedValue(BATCH_ROWS);

    renderImports(false);
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));

    const commit = await screen.findByRole("button", { name: /Commit 1 new transaction/ });
    expect(noticeCount()).toBe(0);
    fireEvent.click(commit);
    await waitFor(() => expect(h.commitBatch).toHaveBeenCalledTimes(1));
  });

  it("a forced form submit cannot reach a gated write through Enter-key submit", async () => {
    renderImports(true);
    const form = await stageUpload("1");
    fireEvent.submit(form);
    expect(h.importTrades).not.toHaveBeenCalled();
    expect(h.uploadImport).not.toHaveBeenCalled();
    expect(noticeFor("import:trades")).not.toBeNull();
  });
});

// ── the copy contract itself ────────────────────────────────────────────────

describe("ServerSideBlocked (contract)", () => {
  it("every capability string names the capability and offers no remedy", async () => {
    const { SERVER_SIDE_BLOCK_COPY } = await import("../src/components/e2ee/ServerSideBlocked");
    for (const [capability, copy] of Object.entries(SERVER_SIDE_BLOCK_COPY)) {
      expect(copy, capability).toContain("is unavailable while client-side encryption is enabled");
      expect(copy, capability).toContain("runs server-side");
      expect(copy, capability).not.toMatch(/unlock/i);
      expect(copy, capability).not.toContain("Settings →");
      // Nothing has been submitted when the gate renders, so the server's
      // trailing "…so nothing was saved" must NOT ride along.
      expect(copy, capability).not.toContain("nothing was saved");
    }
  });

  it("every capability phrase is VERBATIM the backend's refusal phrase (reads the routers)", async () => {
    // The copy's own claim (docs/09 §Server-side writes) is that each string
    // is the endpoint's detail MINUS only its trailing "…so nothing was saved"
    // — so pin it against the SOURCE, not against a copied literal: read the
    // four routers at test time and require each phrase to appear there,
    // quoted, in the message the guard raises. A paraphrase on either side, or
    // a capability added on one side only, goes red here instead of drifting
    // (security findings F3 / F4).
    // `import.meta.url` is an http: URL under the jsdom environment, so fall
    // back to the runner's cwd (the gate runs vitest from `frontend/`).
    const candidates = [
      join(process.cwd(), "..", "backend", "app", "api"),
      (() => {
        try {
          return dirname(fileURLToPath(new URL("../../backend/app/api/", import.meta.url)));
        } catch {
          return "";
        }
      })(),
    ].filter(Boolean);
    const apiDir = candidates.find(existsSync) ?? "";
    expect(apiDir, "backend/app/api not found from the test runner's cwd").not.toBe("");
    const routers = ["imports.py", "trades.py", "sync.py", "ynab.py"];
    // Compare with quotes and whitespace stripped: routers split a message
    // across adjacent string literals (sync.py), so the raw source never holds
    // the phrase as one quoted token, while the words themselves must match.
    const flatten = (text: string) => text.replace(/[\s"]/g, "");
    const sources = routers.map((name) => {
      const source = readFileSync(join(apiDir, name), "utf8");
      expect(source, name).not.toBe(""); // never compare phrases against an empty read
      return flatten(source);
    });
    const { SERVER_SIDE_BLOCK_COPY } = await import("../src/components/e2ee/ServerSideBlocked");
    const entries = Object.entries(SERVER_SIDE_BLOCK_COPY);
    expect(entries.length).toBeGreaterThanOrEqual(8);
    for (const [capability, copy] of entries) {
      expect(copy, capability).toContain(" is unavailable");
      const phrase = copy.split(" is unavailable")[0];
      expect(phrase, capability).not.toBe("");
      const found = sources.some((source) => source.includes(flatten(phrase)));
      expect(found, `${capability}: no router refuses with "${phrase}"`).toBe(true);
    }
  });

  it("does not add a fifth lock-state constant to VaultLocked's four (rule 7g)", async () => {
    const { VAULT_LOCK_COPY } = await import("../src/components/e2ee/VaultLocked");
    expect(Object.keys(VAULT_LOCK_COPY).sort()).toEqual(
      ["client", "search", "status", "write"].sort(),
    );
  });
});

// M112 — the Column-Mapping Wizard's auto-launch must key off the error
// envelope's machine-readable `code`, never its human `detail`.
//
// Named after the dead branch found while live-verifying M111 on prod: for an
// account whose institution has no `importer_profile` the server answers
//   422 {"detail": "institution has no importer_profile for CSV import",
//        "code": "missing_importer_profile"}
// but `apiErrorMessage(err)` returns `ApiError.message`, and `ApiError` does
// `super(detail)` — so the old
//   msg.includes("missing_importer_profile") || msg.includes("No importer profile")
// test could not match either token (the second string exists nowhere in the
// backend). The 422 fell through to `toast.error("Preview failed")`, `window.__net`
// showed no `POST /imports/suggest-profile`, and the wizard never opened, while
// docs/21 documented the auto-launch as working.
//
// Two entry points carry the branch and both must open the wizard:
//   1. `runPreview.onError` — the "Preview import" click (bank statement).
//   2. `upload.onError`     — "Confirm import of N rows" after a dry run.
//
// The detail-vs-code confusion is pinned by name in BOTH directions:
//   - detail WITHOUT the token + code WITH it → wizard opens (the prod case);
//   - detail WITH the token + code WITHOUT it → wizard does NOT open.
import { fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({
  restoreSession: vi.fn(),
  me: vi.fn(),
  listAccounts: vi.fn(),
  listInstitutions: vi.fn(),
  listImports: vi.fn(),
  previewImport: vi.fn(),
  suggestProfile: vi.fn(),
  uploadImport: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, restoreSession: h.restoreSession, me: h.me };
});
vi.mock("../src/api/accounts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/accounts")>();
  return { ...actual, listAccounts: h.listAccounts };
});
vi.mock("../src/api/institutions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/institutions")>();
  return { ...actual, listInstitutions: h.listInstitutions };
});
vi.mock("../src/api/imports", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/imports")>();
  return {
    ...actual,
    listImports: h.listImports,
    previewImport: h.previewImport,
    suggestProfile: h.suggestProfile,
    uploadImport: h.uploadImport,
  };
});

import { ApiError } from "../src/api/client";
import { ToastProvider } from "../src/components/ui/Toast";
import { isMissingImporterProfile, Imports } from "../src/pages/Imports";
import type { DryRunResponse, SuggestProfileResponse } from "../src/api/imports";

/** The real backend text (csv_profile.MissingProfileError) — contains NO code token. */
const DETAIL_WITHOUT_CODE = "institution has no importer_profile for CSV import";
const MISSING_PROFILE = "missing_importer_profile";

const SUGGESTION: SuggestProfileResponse = {
  columns: { date: "Date", raw_description: "Description", amount_native: "Amount" },
  date_format: "%Y-%m-%d",
  decimal_comma: false,
  credits_positive: true,
  confidence: 0.9,
  reasoning: "Header names match",
};

const DRY_RUN: DryRunResponse = {
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

const USER = {
  id: "user-1",
  email: "demo@fintracker.local",
  privacy_mode: false,
  currency_tracker_enabled: false,
  llm_provider: "openai",
  llm_base_url: null,
  llm_model: "gpt-4o-mini",
  encryption_required: false,
  dashboard_preset: "overview",
};

/** An institution with NO importer_profile — the case that 422s. */
const UNPROFILED = { id: 3, name: "Unprofiled Bank", importer_profile: null };

const CHEQUING = {
  id: 7,
  institution_id: 3,
  name: "Everyday chequing",
  type: "chequing" as const,
  currency: "CAD",
  taxable: false,
  llm_autopilot: false,
  cash_balance: "100.00",
  investment_value: "0.00",
  total_value: "100.00",
  starting_balance: "0.00",
  name_cipher: null,
};

function page<T>(items: T[]) {
  return { items, total: items.length, limit: 200, offset: 0 };
}

/** jsdom has no matchMedia (ThemeProvider/framer-motion read prefers-color-scheme). */
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

/** Render the page exactly as the shell does. */
function renderPage(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/imports"]}>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <Routes>
            <Route path="*" element={ui} />
          </Routes>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** Stage the upload form: real File in the real input + a selected account. */
async function stageUpload() {
  renderPage(<Imports />);
  // The account <select> only offers options once the accounts query lands.
  await screen.findByText(/Everyday chequing \(CAD\)/);
  const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
  if (!fileInput) throw new Error("no file input in the upload form");
  fireEvent.change(fileInput, {
    target: { files: [new File(["date,description,amount\n1/2/2024,COFFEE,-12.50\n"], "statement.csv", { type: "text/csv" })] },
  });
  const form = fileInput.closest("form");
  if (!form) throw new Error("file input is not inside a form");
  const accountSelect = form.querySelector("select");
  if (!accountSelect) throw new Error("no account <select> in the upload form");
  fireEvent.change(accountSelect, { target: { value: "7" } });
  return form;
}

/** Submit the staged form through the real submit button ("Preview import"). */
function submitPreview(form: HTMLFormElement) {
  const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]');
  if (!submit) throw new Error("no submit button");
  fireEvent.click(submit);
}

beforeEach(() => {
  h.restoreSession.mockResolvedValue({ access_token: "t", token_type: "bearer", user: USER });
  h.me.mockResolvedValue(USER);
  h.listAccounts.mockResolvedValue(page([CHEQUING]));
  h.listInstitutions.mockResolvedValue(page([UNPROFILED]));
  h.listImports.mockResolvedValue(page([]));
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
  h.suggestProfile.mockResolvedValue(SUGGESTION);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("M112 — wizard auto-launch keys off envelope `code`, not `detail`", () => {
  it("runPreview.onError: 422 with detail WITHOUT the token but code WITH it opens the wizard (prod case)", async () => {
    // This is the exact confusion: `apiErrorMessage(err)` renders DETAIL, so the
    // pre-M112 `msg.includes("missing_importer_profile")` branch never fired.
    h.previewImport.mockRejectedValue(
      new ApiError(422, DETAIL_WITHOUT_CODE, MISSING_PROFILE),
    );

    const form = await stageUpload();
    submitPreview(form);

    await waitFor(() => expect(h.suggestProfile).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Column Mapping")).toBeTruthy();
    // The 422 must NOT surface as a generic failure toast.
    expect(screen.queryByText("Preview failed")).toBeNull();
  });

  it("runPreview.onError: detail CONTAINING the token but code WITHOUT it does NOT open the wizard (detail is not the signal)", async () => {
    h.previewImport.mockRejectedValue(
      new ApiError(422, "missing_importer_profile echoed in the detail only", "invalid_profile"),
    );

    const form = await stageUpload();
    submitPreview(form);

    expect(await screen.findByText("Preview failed")).toBeTruthy();
    expect(h.suggestProfile).not.toHaveBeenCalled();
    expect(screen.queryByText("Column Mapping")).toBeNull();
  });

  it("runPreview.onError: any other envelope code still toasts and never calls suggest-profile", async () => {
    h.previewImport.mockRejectedValue(
      new ApiError(422, "Invalid profile: credit column mapping is empty", "invalid_profile"),
    );

    const form = await stageUpload();
    submitPreview(form);

    expect(await screen.findByText("Preview failed")).toBeTruthy();
    expect(h.suggestProfile).not.toHaveBeenCalled();
    expect(screen.queryByText("Column Mapping")).toBeNull();
  });

  it("upload.onError: Confirm after a dry run opens the wizard on the same code (second branch)", async () => {
    h.uploadImport.mockRejectedValue(
      new ApiError(422, DETAIL_WITHOUT_CODE, MISSING_PROFILE),
    );

    const form = await stageUpload();
    submitPreview(form);
    // Preview succeeded → the dry-run panel is up; Confirm re-runs `upload`.
    expect(await screen.findByText("Preview — nothing is saved yet")).toBeTruthy();
    expect(h.previewImport).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /Confirm import of 1 row/ }));

    await waitFor(() => expect(h.suggestProfile).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Column Mapping")).toBeTruthy();
    expect(screen.queryByText("Upload failed")).toBeNull();
  });

  it("isMissingImporterProfile is exact about detail-vs-code", () => {
    // detail lacks the token, code has it → the case that used to be dead.
    expect(isMissingImporterProfile(new ApiError(422, DETAIL_WITHOUT_CODE, MISSING_PROFILE))).toBe(true);
    // detail has the token, code does not → must not match.
    expect(isMissingImporterProfile(new ApiError(422, "missing_importer_profile", "invalid_profile"))).toBe(false);
    // A code-less envelope (proxy HTML, non-JSON body) must not match.
    expect(isMissingImporterProfile(new ApiError(422, DETAIL_WITHOUT_CODE, null))).toBe(false);
    // A plain Error carrying the token in its message is not the envelope.
    expect(isMissingImporterProfile(new Error(MISSING_PROFILE))).toBe(false);
    expect(isMissingImporterProfile(null)).toBe(false);
  });
});

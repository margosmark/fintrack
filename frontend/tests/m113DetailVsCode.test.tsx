// M113 — detail-vs-code: the client branches on the envelope's `code`, never
// on its human `detail` (docs/08 §Error envelope).
//
// `ApiError` is `{detail, code}` and calls `super(detail)`, so `err.message` —
// and therefore `apiErrorMessage(err)` — is ALWAYS the human copy. Six sites
// tested `err.message.includes("<code>")`, i.e. matched a `code` token against
// a DIFFERENT envelope field: the friendly branch was dead code and the raw
// `detail` shipped to the user instead. The token only ever appeared in
// `detail` by coincidence, so a copy edit flipped the branch's fate.
//
// Same defect class as M112 (the import wizard never auto-opened), found while
// fixing it; cosmetic here, no lost feature. The fix is ONE predicate,
// `hasErrorCode(err, code)` in `frontend/src/api/client.ts`, which M112's
// `isMissingImporterProfile` now wraps too.
//
// The detail-vs-code confusion is pinned by name in BOTH directions for every
// one of the six sites:
//   - detail WITHOUT the token + code WITH it  → the friendly copy renders
//     (this is what the backend actually sends: `errors.py::api_error` keeps
//     them in separate fields);
//   - detail WITH the token + code WITHOUT it  → the friendly copy must NOT
//     render; the raw detail is shown.
// Flip any site back to `err.message.includes(...)` and its pair goes red.
import { readFileSync } from "node:fs";
import path from "node:path";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const ROOT = path.resolve(__dirname, "../src");

const h = vi.hoisted(() => ({
  listTags: vi.fn(),
  createTag: vi.fn(),
  updateTag: vi.fn(),
  deleteTag: vi.fn(),
  mergeTags: vi.fn(),
  listCategories: vi.fn(),
  createCategory: vi.fn(),
  updateCategory: vi.fn(),
  deleteCategory: vi.fn(),
}));

vi.mock("../src/api/tags", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/tags")>()),
  listTags: h.listTags,
  createTag: h.createTag,
  updateTag: h.updateTag,
  deleteTag: h.deleteTag,
  mergeTags: h.mergeTags,
}));

vi.mock("../src/api/categories", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/categories")>()),
  listCategories: h.listCategories,
  createCategory: h.createCategory,
  updateCategory: h.updateCategory,
  deleteCategory: h.deleteCategory,
}));

import { ApiError, hasErrorCode } from "../src/api/client";
import type { Category } from "../src/api/categories";
import { ToastProvider } from "../src/components/ui/Toast";
import { TagManagerModal } from "../src/components/transactions/TagManagerModal";
import { CategoryManager } from "../src/pages/Settings";
import { Tags } from "../src/pages/Tags";

/** jsdom has no matchMedia (framer-motion / ThemeProvider read it). */
beforeAll(() => {
  if (window.matchMedia === undefined) {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }),
    });
  }
});

function page<T>(items: T[]) {
  return { items, total: items.length, limit: 200, offset: 0 };
}

function tag(id: number, name: string, transaction_count = 0, rule_count = 0) {
  return { id, user_id: 1, name, transaction_count, rule_count };
}

const DUPLICATE = "A tag with that name already exists.";
const TAG_IN_USE_TAGS = "Tag is in use by transactions or rules — remove it first.";
const TAG_IN_USE_MANAGER = "That tag is in use — remove it from its transactions or rules first.";
const CATEGORY_IN_USE =
  "Category is in use by transactions, subcategories, rules or learned payees — reassign first.";

function renderWithProviders(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ToastProvider>{ui}</ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** Submit the "New tag name" form on the Tags page through its real button. */
async function submitNewTag(name: string) {
  renderWithProviders(<Tags />);
  const input = await screen.findByPlaceholderText("e.g. recurring, tax-deductible");
  fireEvent.change(input, { target: { value: name } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
}

/** Open the rename editor on the single chip of the Tags page, then save. */
async function renameViaTagsPage(next: string) {
  renderWithProviders(<Tags />);
  await screen.findByText("Groceries");
  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  fireEvent.change(screen.getByLabelText("Tag name"), { target: { value: next } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

async function deleteViaTagsPage() {
  renderWithProviders(<Tags />);
  await screen.findByText("Groceries");
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
}

beforeEach(() => {
  h.listTags.mockResolvedValue(page([tag(42, "Groceries")]));
  h.createTag.mockResolvedValue({ id: 99, user_id: 1, name: "Groceries" });
  h.updateTag.mockResolvedValue({ id: 42, user_id: 1, name: "Groceries" });
  h.deleteTag.mockResolvedValue(undefined);
  h.mergeTags.mockResolvedValue({ target: { id: 42, user_id: 1, name: "Groceries" }, merged_tag_ids: [] });
  h.listCategories.mockResolvedValue(page([category(1, "Groceries")]));
  h.createCategory.mockResolvedValue(category(2, "New"));
  h.updateCategory.mockResolvedValue(category(1, "Groceries"));
  h.deleteCategory.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function category(id: number, name: string): Category {
  return { id, user_id: 1, name, parent_id: null, is_system: false };
}

describe("M113 — detail-vs-code: one predicate, keyed off the envelope `code`", () => {
  it("detail-vs-code: hasErrorCode reads `code` only and ignores `detail` entirely", () => {
    // The real backend envelope (tags.py): detail is human copy, no code token.
    expect(hasErrorCode(new ApiError(409, "Tag name already exists", "duplicate_tag"), "duplicate_tag")).toBe(true);
    // The inverse: the token in the detail under another code must NOT match.
    expect(hasErrorCode(new ApiError(409, "duplicate_tag leaked into the detail", "validation_error"), "duplicate_tag")).toBe(false);
    // A code-less envelope (proxy HTML, non-JSON body) and non-envelope errors.
    expect(hasErrorCode(new ApiError(409, "Tag name already exists", null), "duplicate_tag")).toBe(false);
    expect(hasErrorCode(new Error("duplicate_tag"), "duplicate_tag")).toBe(false);
    expect(hasErrorCode(null, "duplicate_tag")).toBe(false);
  });

  describe("Tags page (create / rename / delete)", () => {
    it("detail-vs-code — create: the real duplicate_tag envelope renders the friendly copy", async () => {
      h.createTag.mockRejectedValue(new ApiError(409, "Tag name already exists", "duplicate_tag"));

      await submitNewTag("Groceries");

      expect(await screen.findByText(DUPLICATE)).toBeTruthy();
      expect(screen.getByText("Could not create tag")).toBeTruthy();
    });

    it("detail-vs-code — create: a detail that CONTAINS the token under another code does not take the branch", async () => {
      const detail = "duplicate_tag mentioned only in the detail";
      h.createTag.mockRejectedValue(new ApiError(422, detail, "validation_error"));

      await submitNewTag("Groceries");

      expect(await screen.findByText(detail)).toBeTruthy();
      expect(screen.queryByText(DUPLICATE)).toBeNull();
    });

    it("detail-vs-code — rename: the real duplicate_tag envelope renders the friendly copy", async () => {
      h.updateTag.mockRejectedValue(new ApiError(409, "Tag name already exists", "duplicate_tag"));

      await renameViaTagsPage("Groceries");

      expect(await screen.findByText(DUPLICATE)).toBeTruthy();
      expect(screen.getByText("Rename failed")).toBeTruthy();
    });

    it("detail-vs-code — rename: a detail that CONTAINS the token under another code does not take the branch", async () => {
      const detail = "duplicate_tag mentioned only in the detail";
      h.updateTag.mockRejectedValue(new ApiError(422, detail, "validation_error"));

      await renameViaTagsPage("Groceries");

      expect(await screen.findByText(detail)).toBeTruthy();
      expect(screen.queryByText(DUPLICATE)).toBeNull();
    });

    it("detail-vs-code — delete: the real tag_in_use envelope renders the friendly copy", async () => {
      h.deleteTag.mockRejectedValue(new ApiError(409, "Tag is in use and cannot be deleted", "tag_in_use"));

      await deleteViaTagsPage();

      expect(await screen.findByText(TAG_IN_USE_TAGS)).toBeTruthy();
      expect(screen.getByText("Delete blocked")).toBeTruthy();
    });

    it("detail-vs-code — delete: a detail that CONTAINS the token under another code does not take the branch", async () => {
      const detail = "tag_in_use mentioned only in the detail";
      h.deleteTag.mockRejectedValue(new ApiError(422, detail, "validation_error"));

      await deleteViaTagsPage();

      expect(await screen.findByText(detail)).toBeTruthy();
      expect(screen.queryByText(TAG_IN_USE_TAGS)).toBeNull();
    });
  });

  describe("TagManagerModal (rename / delete)", () => {
    it("detail-vs-code — rename: the real duplicate_tag envelope renders the friendly copy", async () => {
      h.listTags.mockResolvedValue(page([tag(41, "Goceries")]));
      h.updateTag.mockRejectedValue(new ApiError(409, "Tag name already exists", "duplicate_tag"));

      renderWithProviders(<TagManagerModal onClose={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Rename Goceries" }));
      fireEvent.change(screen.getByLabelText("New name for Goceries"), { target: { value: "Groceries" } });
      fireEvent.click(screen.getByRole("button", { name: "Save name" }));

      expect(await screen.findByText(DUPLICATE)).toBeTruthy();
      expect(screen.getByText("Rename failed")).toBeTruthy();
    });

    it("detail-vs-code — rename: a detail that CONTAINS the token under another code does not take the branch", async () => {
      h.listTags.mockResolvedValue(page([tag(41, "Goceries")]));
      const detail = "duplicate_tag mentioned only in the detail";
      h.updateTag.mockRejectedValue(new ApiError(422, detail, "validation_error"));

      renderWithProviders(<TagManagerModal onClose={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Rename Goceries" }));
      fireEvent.change(screen.getByLabelText("New name for Goceries"), { target: { value: "Groceries" } });
      fireEvent.click(screen.getByRole("button", { name: "Save name" }));

      expect(await screen.findByText(detail)).toBeTruthy();
      expect(screen.queryByText(DUPLICATE)).toBeNull();
    });

    it("detail-vs-code — delete: the real tag_in_use envelope renders the friendly copy", async () => {
      // Counts say "unused" (so the button exists) while the server refuses —
      // the 409 backstop docs/09 §Tag UX documents.
      h.deleteTag.mockRejectedValue(new ApiError(409, "Tag is in use and cannot be deleted", "tag_in_use"));

      renderWithProviders(<TagManagerModal onClose={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Delete Groceries" }));

      expect(await screen.findByText(TAG_IN_USE_MANAGER)).toBeTruthy();
      expect(screen.getByText("Delete blocked")).toBeTruthy();
    });

    it("detail-vs-code — delete: a detail that CONTAINS the token under another code does not take the branch", async () => {
      const detail = "tag_in_use mentioned only in the detail";
      h.deleteTag.mockRejectedValue(new ApiError(422, detail, "validation_error"));

      renderWithProviders(<TagManagerModal onClose={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Delete Groceries" }));

      expect(await screen.findByText(detail)).toBeTruthy();
      expect(screen.queryByText(TAG_IN_USE_MANAGER)).toBeNull();
    });
  });

  describe("Settings → CategoryManager (delete)", () => {
    it("detail-vs-code — delete: the real category_in_use envelope renders the friendly copy", async () => {
      h.deleteCategory.mockRejectedValue(
        new ApiError(409, "Category is in use; reassign references first", "category_in_use"),
      );

      renderWithProviders(<CategoryManager />);
      fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

      // BOTH channels the branch feeds carry this copy (inline action error +
      // toast description), so it is a findAllByText: a getBy/findBy on a
      // duplicated string fails on "found multiple elements" and would only
      // resolve once the toast auto-dismisses.
      expect((await screen.findAllByText(CATEGORY_IN_USE)).length).toBeGreaterThan(1);
      expect(screen.getByText("Delete blocked")).toBeTruthy();
    });

    it("detail-vs-code — delete: a detail that CONTAINS the token under another code does not take the branch", async () => {
      const detail = "category_in_use mentioned only in the detail";
      h.deleteCategory.mockRejectedValue(new ApiError(422, detail, "validation_error"));

      renderWithProviders(<CategoryManager />);
      fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

      expect((await screen.findAllByText(detail)).length).toBeGreaterThan(1);
      expect(screen.queryByText(CATEGORY_IN_USE)).toBeNull();
    });
  });

  it("detail-vs-code — M112's wrapper still keys off `code` through the shared predicate", () => {
    // isMissingImporterProfile is now a thin wrapper over hasErrorCode; this
    // pins that the shared predicate still carries the M112 contract.
    const detailWithoutCode = "institution has no importer_profile for CSV import";
    expect(hasErrorCode(new ApiError(422, detailWithoutCode, "missing_importer_profile"), "missing_importer_profile")).toBe(true);
    expect(hasErrorCode(new ApiError(422, "missing_importer_profile in the detail only", "invalid_profile"), "missing_importer_profile")).toBe(false);
  });

  it("detail-vs-code — every converted site is keyed off hasErrorCode in source", () => {
    // Source tripwire: an `err.message.includes("<code>")` branch may not
    // return anywhere on these three surfaces, and each code token must be
    // fed to hasErrorCode.
    const files = [
      "pages/Tags.tsx",
      "pages/Settings.tsx",
      "components/transactions/TagManagerModal.tsx",
    ];
    for (const rel of files) {
      const src = readFileSync(path.join(ROOT, rel), "utf8");
      expect(src, `${rel} must not match err.message against a code token`).not.toMatch(
        /message\.includes\("(duplicate_tag|tag_in_use|category_in_use)"\)/,
      );
      expect(src, `${rel} must branch through hasErrorCode`).toMatch(
        /hasErrorCode\(err, "(duplicate_tag|tag_in_use|category_in_use)"\)/,
      );
    }
  });
});

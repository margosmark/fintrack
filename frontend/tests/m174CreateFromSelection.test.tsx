/**
 * M174 — create from a SELECTION, and the tags that mark recurring.
 *
 * Three contracts:
 *
 *  1. **The criteria come from the shared normalize, not from the first row.**
 *     A selection of one merchant's spelling variants must produce ONE payee
 *     pattern that `contains`-matches every selected row — on the rule engine
 *     (case-insensitive substring over `normalized_payee`) and on the bill
 *     matcher (whitespace-free key on both sides). A pattern that matched only
 *     the first spelling would recreate the M165 manufactured duplicate one
 *     layer up, in the editors.
 *  2. **A selection that cannot express one pattern says so.** Two merchants is
 *     a blocked target with the reason printed, never a dead click and never a
 *     guessed pattern.
 *  3. **The Settings control writes the WHOLE list.** `recurring_intent_tag_ids`
 *     is a preference like `saved_report_views` — one PATCH, `[]` = the feature
 *     is off, and the card says "off" in words rather than leaving the user to
 *     infer it from a calendar with no dots.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const toastSpies = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
}));

vi.mock("../src/components/ui/Toast", () => ({ useToast: () => toastSpies }));

vi.mock("../src/api/transactions", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/api/transactions")>();
  return { ...mod, updateTransaction: vi.fn().mockResolvedValue({}) };
});

vi.mock("../src/api/client", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/api/client")>();
  return { ...mod, me: vi.fn() };
});

vi.mock("../src/api/tags", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/api/tags")>();
  return { ...mod, listTags: vi.fn() };
});

vi.mock("../src/api/users", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/api/users")>();
  return { ...mod, updateMe: vi.fn() };
});

import { me } from "../src/api/client";
import { listTags } from "../src/api/tags";
import { updateMe } from "../src/api/users";
import type { Transaction } from "../src/api/transactions";
import { BulkActionsToolbar } from "../src/components/transactions/BulkActionsToolbar";
import { RecurringIntentSection } from "../src/components/settings/RecurringIntentSection";
import {
  MAX_SELECTION_PATTERN,
  selectionCriteria,
} from "../src/lib/createFromSearch";

const CATEGORY = { id: 4, user_id: 1, name: "Groceries", parent_id: null, is_system: false };
const TAGS = [
  { id: 9, user_id: 1, name: "vacation", transaction_count: 2, rule_count: 0 },
  { id: 11, user_id: 1, name: "subscription", transaction_count: 3, rule_count: 0 },
];

function tx(
  id: number,
  payee: string,
  categoryId: number | null = null,
): Transaction {
  return {
    id,
    account_id: 7,
    date: "2026-09-01",
    posting_date: null,
    raw_description: `${payee} RAW`,
    normalized_payee: payee,
    amount_native: "-12.00",
    currency: "CAD",
    cad_amount: "-12.00",
    category_id: categoryId,
    transfer_pair_id: null,
    pair_account_name: null,
    tags: [],
    payee_cipher: null,
    raw_desc_cipher: null,
    amount_cipher: null,
    cad_amount_cipher: null,
    payee_token: null,
  } as unknown as Transaction;
}

function withProviders(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>{node}</QueryClientProvider>
    </MemoryRouter>,
  );
}

function mountToolbar({
  rows,
  selectedIds,
  matched = null,
}: {
  rows: Transaction[];
  selectedIds: number[];
  matched?: { filter: Record<string, never>; excludeIds: number[]; total: number } | null;
}) {
  withProviders(
    <BulkActionsToolbar
      rows={rows}
      selectedIds={selectedIds}
      matched={matched}
      matchedTotal={matched?.total ?? null}
      categories={[CATEGORY]}
      tags={TAGS}
      onClear={vi.fn()}
      onSettled={vi.fn()}
    />,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

// --- the derivation ---------------------------------------------------------

describe("M174 — criteria from a selection (docs/09 §Create from a selection)", () => {
  it("collapses M165's two YouTube spellings into ONE pattern covering both", () => {
    // The M165 fixture verbatim: one merchant, two statement spellings. The
    // shared `normalizePayeeKey` says they are one merchant; the derived
    // pattern has to be a `contains` hit on BOTH, which is the assertion that
    // matters — a pattern matching only the first row is the defect this
    // exists to prevent.
    const rows = [tx(1, "GOOGLE *YouTubePremium"), tx(2, "Google*Youtubepremium")];
    const state = selectionCriteria(rows);

    expect(state.merchants).toBe(1);
    expect(state.pattern).not.toBeNull();
    for (const row of rows) {
      expect(row.normalized_payee!.toLowerCase()).toContain(
        state.pattern!.toLowerCase(),
      );
    }
    expect(state.criteria).toEqual({ q: state.pattern });
  });

  it("also covers a trailing store-number variant (the second normalize rule)", () => {
    const rows = [tx(1, "SUPERSTORE 1234"), tx(2, "Superstore #1234")];
    const state = selectionCriteria(rows);
    expect(state.merchants).toBe(1);
    expect(state.pattern).not.toBeNull();
    for (const row of rows) {
      expect(row.normalized_payee!.toLowerCase()).toContain(state.pattern!.toLowerCase());
    }
  });

  it("refuses a pattern for a two-merchant selection instead of inventing one", () => {
    const state = selectionCriteria([tx(1, "NETFLIX.COM"), tx(2, "SEAFOOD CITY SUPERMARK")]);
    expect(state.merchants).toBe(2);
    expect(state.pattern).toBeNull();
    expect(state.criteria).toEqual({});
  });

  it("carries a category only when every selected row agrees on one", () => {
    expect(selectionCriteria([tx(1, "NETFLIX.COM", 4), tx(2, "Netflix.com", 4)]).criteria).toEqual(
      { q: expect.any(String), category: "4" },
    );
    expect(
      selectionCriteria([tx(1, "NETFLIX.COM", 4), tx(2, "Netflix.com", null)]).criteria.category,
    ).toBeUndefined();
  });

  it("has nothing to derive from rows with no payee at all", () => {
    const blank = [{ ...tx(1, ""), raw_description: "" }, { ...tx(2, ""), raw_description: "" }];
    const state = selectionCriteria(blank);
    expect(state.merchants).toBe(0);
    expect(state.pattern).toBeNull();
    expect(state.criteria).toEqual({});
  });

  it("treats a payee that carries its OWN date as two merchants, and says so", () => {
    // The shared normalize strips ONE trailing number run, not a date: the
    // honest outcome for `… 2025-12-31` vs `… 2026-10-04` is a refused
    // selection, not a pattern ("SEAFOOD CITY SUPERMARK 202") that would
    // silently match a third merchant. The control prints the reason
    // (docs/09 §Create from a selection).
    const state = selectionCriteria([
      tx(1, "SEAFOOD CITY SUPERMARK 2025-12-31"),
      tx(2, "SEAFOOD CITY SUPERMARK 2026-10-04"),
    ]);
    expect(state.merchants).toBe(2);
    expect(state.pattern).toBeNull();
  });

  it("never emits a pattern longer than the server's term cap", () => {
    const long = "MERCHANT " + "X".repeat(200);
    const state = selectionCriteria([tx(1, long), tx(2, long.toUpperCase())]);
    expect(state.pattern!.length).toBeLessThanOrEqual(MAX_SELECTION_PATTERN);
  });
});

// --- the toolbar control ----------------------------------------------------

describe("M174 — the bulk toolbar's create menu (docs/09 §Bulk selection toolbar)", () => {
  // Mark's case: two SEAFOOD CITY SUPERMARK rows ticked in the ledger. The
  // dates live on the ROWS, not in the payee, so the shared normalize says one
  // merchant and the created rule/bill covers both charges.
  const SEAFOOD = [tx(1, "SEAFOOD CITY SUPERMARK"), tx(2, "SEAFOOD CITY SUPERMARK")];

  function openCreate() {
    fireEvent.click(screen.getByRole("button", { name: /Create from selection/ }));
    return screen.getByRole("menu", { name: "Create from the selected rows" });
  }

  it("is a menu by ARIA, opens focused, and Escape returns focus to the trigger", () => {
    mountToolbar({ rows: SEAFOOD, selectedIds: [1, 2] });
    const trigger = screen.getByRole("button", { name: /Create from selection/ });
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");

    const menu = openCreate();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(trigger.getAttribute("aria-controls")).toBe(menu.id);
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: /Rule from these 2/ }));

    fireEvent.keyDown(document.activeElement as Element, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: /Bill from these 2/ }));

    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("deep-links rule and bill with the SAME ?prefill criteria M123 emits", () => {
    mountToolbar({ rows: SEAFOOD, selectedIds: [1, 2] });
    openCreate();

    const pattern = selectionCriteria(SEAFOOD).pattern!;
    const rule = screen.getByRole("menuitem", { name: /Rule from these 2/ }) as HTMLAnchorElement;
    const bill = screen.getByRole("menuitem", { name: /Bill from these 2/ }) as HTMLAnchorElement;

    for (const link of [rule, bill]) {
      const [, query = ""] = (link.getAttribute("href") ?? "").split("?");
      const params = new URLSearchParams(query);
      expect(params.get("prefill")).toBe("transactions");
      expect(params.get("q")).toBe(pattern);
      // The pattern really does cover both rows, whichever editor reads it.
      for (const row of SEAFOOD) {
        expect(row.normalized_payee!.toLowerCase()).toContain(
          params.get("q")!.toLowerCase(),
        );
      }
    }
    expect(rule.getAttribute("href")).toMatch(/^\/rules\?/);
    expect(bill.getAttribute("href")).toMatch(/^\/bills\?/);
  });

  it("blocks a multi-merchant selection with the reason, and never as a link", () => {
    mountToolbar({
      rows: [tx(1, "NETFLIX.COM"), tx(2, "SEAFOOD CITY SUPERMARK")],
      selectedIds: [1, 2],
    });
    openCreate();

    for (const name of [/Rule from these 2/, /Bill from these 2/]) {
      const item = screen.getByRole("menuitem", { name });
      expect(item.getAttribute("aria-disabled")).toBe("true");
      expect(item.tagName).not.toBe("A");
      expect(item.textContent).toContain("2 payees selected — pick rows from one merchant");
    }
  });

  it("offers nothing on the MATCHED path — that set carries no rows", () => {
    mountToolbar({
      rows: SEAFOOD,
      selectedIds: [],
      matched: { filter: {}, excludeIds: [], total: 345 },
    });
    expect(screen.queryByRole("button", { name: /Create from selection/ })).toBeNull();
    // …while the ordinary bulk actions are still there.
    expect(screen.getByRole("button", { name: "Recategorize" })).toBeTruthy();
  });

  it("keeps the selection's create control beside the count, not behind a mode", () => {
    // The control is in the IDLE toolbar — no mode has to be entered first,
    // which is what makes it reachable on a phone without hunting.
    mountToolbar({ rows: SEAFOOD, selectedIds: [1, 2] });
    const toolbar = screen.getByText("2 selected").parentElement!;
    expect(toolbar.contains(screen.getByRole("button", { name: /Create from selection/ }))).toBe(
      true,
    );
  });
});

// --- Settings ---------------------------------------------------------------

describe("M174 — Settings: tags that mark recurring", () => {
  function mountSettings(ids: number[] = []) {
    vi.mocked(me).mockResolvedValue({
      id: "1",
      email: "mark@example.com",
      privacy_mode: false,
      currency_tracker_enabled: true,
      llm_provider: "ollama",
      llm_base_url: null,
      llm_model: "llama3",
      encryption_required: false,
      dashboard_preset: "overview",
      saved_report_views: [],
      recurring_intent_tag_ids: ids,
    });
    vi.mocked(listTags).mockResolvedValue({ items: TAGS, total: 2, limit: 200, offset: 0 });
    withProviders(<RecurringIntentSection />);
  }

  beforeEach(() => {
    vi.mocked(updateMe).mockImplementation(async (patch) => ({
      id: "1",
      email: "mark@example.com",
      privacy_mode: false,
      currency_tracker_enabled: true,
      llm_provider: "ollama",
      llm_base_url: null,
      llm_model: "llama3",
      encryption_required: false,
      dashboard_preset: "overview",
      saved_report_views: [],
      recurring_intent_tag_ids: patch.recurring_intent_tag_ids ?? [],
    }));
  });

  it("writes the WHOLE list in one PATCH, and says the feature is OFF when empty", async () => {
    mountSettings([]);
    expect(await screen.findByText("Off — no tag marks a subscription.")).toBeTruthy();

    fireEvent.click(await screen.findByRole("button", { name: "subscription" }));
    expect(screen.getByText("1 selected")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save tags" }));

    await waitFor(() => expect(updateMe).toHaveBeenCalledTimes(1));
    expect(vi.mocked(updateMe).mock.calls[0][0]).toEqual({ recurring_intent_tag_ids: [11] });
    expect(await screen.findByText("1 selected")).toBeTruthy();
  });

  it("states that no tag name is interpreted — the user names the tags", async () => {
    mountSettings([]);
    await screen.findByRole("button", { name: "subscription" });
    expect(
      screen.getByText(/Nothing is inferred from a tag’s name/),
    ).toBeTruthy();
  });

  it("sends the same set back when a nominated tag is unselected", async () => {
    mountSettings([11]);
    const tag = await screen.findByRole("button", { name: "subscription" });
    expect(tag.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(tag);
    fireEvent.click(screen.getByRole("button", { name: "Save tags" }));

    await waitFor(() => expect(updateMe).toHaveBeenCalledTimes(1));
    expect(vi.mocked(updateMe).mock.calls[0][0]).toEqual({ recurring_intent_tag_ids: [] });
  });

  it("says where to make a tag when the tenant has none", async () => {
    vi.mocked(listTags).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(me).mockResolvedValue({
      id: "1",
      email: "mark@example.com",
      privacy_mode: false,
      currency_tracker_enabled: true,
      llm_provider: "ollama",
      llm_base_url: null,
      llm_model: "llama3",
      encryption_required: false,
      dashboard_preset: "overview",
      saved_report_views: [],
      recurring_intent_tag_ids: [],
    });
    withProviders(<RecurringIntentSection />);
    expect(await screen.findByText(/You have no tags yet/)).toBeTruthy();
  });
});

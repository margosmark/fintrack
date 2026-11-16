// M119 — the Similar-Past-Entries popover printed "No similar past entries"
// while the same table showed NINE identical payees.
//
// Root cause (proved in the deployed page first, see
// scripts/a11y/m119_similar_repro.py): `SimilarExamples` passed
// `categorizedOnly: true`, so when the anchor was itself the ONLY categorized
// row of its payee family, `excludeId` removed the one qualifying row and the
// filter dropped every identical-payee sibling. The matcher never had a
// chance — the empty state was a filter artifact, not a measurement.
//
// M137 (docs/33 §Server-first computing): the popover is a thin read of the
// re-mounted `GET /transactions/similar` — ranking, `exclude_id` and the
// categorized-first tiebreak are the SERVER's (the same pg_trgm predicate
// `services/categorization` scores with). This file pins the RENDERING of
// that answer: order preserved, inert uncategorized rows, the one-click
// apply path, and the M136 lock honesty driven by the server's `withheld`.
//
// What this file pins:
//   1  the anchor being the only categorized twin no longer empties the list
//   2  uncategorized siblings ARE listed (they are the picture the user is
//      being shown), as inert rows — never buttons that swallow a click
//   3  a categorized sibling ranks FIRST (server order is preserved as-is)
//   4  the one-click apply path still PATCHes the ANCHOR's category
//   5  a locked vault with withheld cipher rows says "Unlock your vault…"
//      instead of claiming there are no similar entries
//   6  plaintext hits + withheld siblings — hits render, no unlock nag

import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";

// jsdom has no matchMedia; ToastProvider asks framer-motion for it.
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

const m = vi.hoisted(() => ({
  getSimilarTransactions: vi.fn(),
  updateTransaction: vi.fn(),
}));

vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  getSimilarTransactions: m.getSimilarTransactions,
  updateTransaction: m.updateTransaction,
}));

import type { SimilarTransaction } from "../src/api/transactions";
import { SimilarExamples, type SimilarAnchor } from "../src/components/transactions/SimilarExamples";
import { ToastProvider } from "../src/components/ui/Toast";

const POOL = 3847;
const PAYEE = "Total Tech Pools And Leis";

/** Mark's ten rows (account 262), ids and dates kept from the report. */
const IDS = [6919, 6921, 6984, 7007, 7088, 7098, 7125, 7139, 7140, 7148];
const DATES = [
  "2026-09-25",
  "2026-09-23",
  "2026-08-19",
  "2026-08-07",
  "2026-06-17",
  "2026-06-11",
  "2026-05-26",
  "2026-05-19",
  "2026-05-19",
  "2026-05-16",
];

function item(over: Partial<SimilarTransaction> & { id: number }): SimilarTransaction {
  return {
    account_id: 262,
    date: "2026-09-25",
    raw_description: PAYEE,
    normalized_payee: PAYEE,
    amount_native: "-11.24",
    currency: "CAD",
    cad_amount: "-11.24",
    category_id: null,
    category_name: null,
    score: 1,
    encrypted: false,
    ...over,
  };
}

/** The server's answer shape: anchor EXCLUDED, categorized-first (the M119
 *  tiebreak), id desc within each group — the popover renders it as-is. */
function family(excludeId: number): SimilarTransaction[] {
  const rows = IDS.filter((id) => id !== excludeId).map((id) =>
    item({
      id,
      date: DATES[IDS.indexOf(id)],
      category_id: id === 6919 ? POOL : null,
      category_name: id === 6919 ? "pool" : null,
    }),
  );
  const categorized = rows.filter((r) => r.category_id !== null);
  const rest = rows.filter((r) => r.category_id === null);
  return [...categorized, ...rest];
}

function page(rows: SimilarTransaction[], withheld = 0) {
  return { query: PAYEE, items: rows, total: rows.length, withheld };
}

function anchor(over: Partial<SimilarAnchor> & { id: number }): SimilarAnchor {
  return { payee: PAYEE, left: 100, top: 200, ...over };
}

function renderPopover(a: SimilarAnchor): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const ui: ReactElement = (
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <SimilarExamples anchor={a} onClose={vi.fn()} />
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>
  );
  render(ui);
}

function dialog(): HTMLElement {
  const el = screen.getByRole("dialog");
  return el as HTMLElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  m.getSimilarTransactions.mockImplementation((query: { excludeId?: number }) =>
    Promise.resolve(page(family(query.excludeId ?? -1))),
  );
  m.updateTransaction.mockResolvedValue(item({ id: 6919, category_id: POOL }));
});

describe("M119 — Similar-Past-Entries starvation", () => {
  it("1: the anchor being the ONLY categorized twin no longer empties the list", async () => {
    // Mark's click: the popover opens from 6919, the row that carries `pool`.
    renderPopover(anchor({ id: 6919 }));

    // Wait for the LIST, not for the absence of the message — asserting
    // "no empty text" first would pass during the `Searching…` frame.
    const items = await waitFor(() => within(dialog()).getAllByRole("listitem"));
    expect(items).toHaveLength(9); // the family is 10 rows, the anchor is one
    expect(within(dialog()).queryByText("No similar past entries")).toBeNull();
    expect(dialog().textContent).toContain(PAYEE);
    // The anchor itself is excluded from its own results (the SERVER excludes
    // it — `exclude_id` travels on the request): 2026-09-25 is 6919's own
    // date and appears nowhere else in the family.
    expect(dialog().textContent).not.toContain("2026-09-25");
    // The request carried the anchor's id — one query, no client re-filter.
    expect(m.getSimilarTransactions).toHaveBeenCalledWith(
      expect.objectContaining({ q: PAYEE, excludeId: 6919, limit: 10 }),
    );
  });

  it("2: uncategorized siblings are listed as inert rows, not dead buttons", async () => {
    renderPopover(anchor({ id: 6919 }));

    const items = await waitFor(() => within(dialog()).getAllByRole("listitem"));
    expect(items).toHaveLength(9);
    // No sibling carries a category, so NONE of them may claim an apply path.
    expect(within(dialog()).queryByTitle(/^Apply/)).toBeNull();
    expect(within(dialog()).getAllByTitle("No category to apply yet")).toHaveLength(9);
    // The dialog's ONLY buttons are Close and "See all similar →" — no row
    // offers an apply affordance it cannot honour.
    expect(within(dialog()).getAllByRole("button").map((b) => b.title)).toEqual([
      "",
      "",
    ]);
  });

  it("3: a categorized sibling ranks FIRST — the server order renders as-is", async () => {
    // Same family, but the anchor is an UNCATEGORIZED row: 6919 is one of the
    // nine survivors. The server ranks categorized-first (the tiebreak M119
    // needs) and the popover renders the answer in ORDER — no client-side
    // re-sort exists to disagree with it.
    renderPopover(anchor({ id: 6921 }));

    const items = await waitFor(() => within(dialog()).getAllByRole("listitem"));
    expect(items).toHaveLength(9);
    const applyable = within(dialog()).getAllByTitle(/^Apply/);
    expect(applyable).toHaveLength(1);
    expect(applyable[0].getAttribute("title")).toBe("Apply “pool”");
    // The applyable row is the FIRST list item, not the last.
    expect(items[0].contains(applyable[0])).toBe(true);
    expect(items[items.length - 1].contains(applyable[0])).toBe(false);
  });

  it("4: one-click apply still PATCHes the ANCHOR's category from the example", async () => {
    renderPopover(anchor({ id: 6921 }));

    const applyable = await waitFor(() => within(dialog()).getByTitle("Apply “pool”"));
    fireEvent.click(applyable);
    await waitFor(() => expect(m.updateTransaction).toHaveBeenCalledTimes(1));
    expect(m.updateTransaction).toHaveBeenCalledWith(6921, { category_id: POOL });
  });

  it("5: a locked vault with withheld siblings says unlock, never 'no similar'", async () => {
    // The popover sent `hide_encrypted: true` (no vault is unlocked in this
    // session), so the SERVER withheld every cipher sibling and reported the
    // count. The empty items alone would make "No similar past entries" a
    // lie about HIDDEN rows, not a measurement of absent ones.
    m.getSimilarTransactions.mockResolvedValue(page([], 10));

    renderPopover(anchor({ id: 6919 }));

    // M136: the wording is no longer the popover's own — it is the shared
    // `VaultLocked` component's ONE search string, plus the canonical
    // destination every lock notice now carries.
    const message = await waitFor(() =>
      screen.getByText(/Unlock the vault to search encrypted payees/),
    );
    expect(message).toBeTruthy();
    expect(screen.getByTestId("vault-locked-destination").textContent).toContain(
      "Settings → Privacy & data → Client-side encryption (in progress)",
    );
    expect(within(dialog()).queryByText("No similar past entries")).toBeNull();
    expect(within(dialog()).queryAllByRole("listitem")).toHaveLength(0);
    // The request is the locked one.
    expect(m.getSimilarTransactions).toHaveBeenCalledWith(
      expect.objectContaining({ hideEncrypted: true }),
    );
  });

  it("6: plaintext hits + withheld siblings — hits render, no unlock nag", async () => {
    // A locked vault alongside plaintext rows: the server answers the nine
    // plaintext hits and reports the two cipher siblings in `withheld`, so
    // the popover must SHOW those hits rather than warn about the ones it
    // cannot read (the withheld count only REPLACES an EMPTY result).
    m.getSimilarTransactions.mockResolvedValue(page(family(6921), 2));

    renderPopover(anchor({ id: 6921 }));

    const items = await waitFor(() => within(dialog()).getAllByRole("listitem"));
    expect(items).toHaveLength(9); // 10 plaintext − the anchor; 0 cipher rows
    expect(within(dialog()).queryByText(/Unlock your vault to search/)).toBeNull();
    expect(within(dialog()).queryByText("No similar past entries")).toBeNull();
    // The categorized plaintext twin still leads.
    expect(within(dialog()).getAllByTitle(/^Apply/)[0].getAttribute("title")).toBe(
      "Apply “pool”",
    );
    // The cipher rows never surface as plaintext.
    expect(dialog().textContent).not.toContain("ciphertext-");
  });
});

// docs/04 §Learning loop — the History drawer's "learned from you" badge.
//
// The marker rides in the RECEIPT PAYLOAD (`payload.learned`, docs/02
// §transaction_events), so the badge is a read of stored data rather than a
// guess about what the summary sentence means:
//   * a correction that taught the learned map wears it;
//   * an edit that taught nothing (a date, a note) does not — the payload has
//     no `learned` key at all, so "absent" and "null" must both read as no;
//   * the badge is ADDITIVE: the summary line and the receipt hash render
//     either way, because the drawer's job is to show the receipt first.
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  listTransactionEvents: vi.fn(),
}));

import { listTransactionEvents } from "../src/api/transactions";
import type { TransactionEvent, TransactionEventPage } from "../src/api/transactions";
import { HistoryDrawer } from "../src/components/transactions/HistoryDrawer";

const HASH = "0123456789abcdef".repeat(4);

function receipt(overrides: Partial<TransactionEvent> = {}): TransactionEvent {
  return {
    id: 11,
    tx_id: 7,
    event_kind: "categorized",
    actor: 3,
    actor_label: "you",
    at: "2026-10-05T12:00:00Z",
    payload_hash: HASH,
    summary: "you set category Subscriptions",
    payload: {},
    ...overrides,
  };
}

function renderDrawer(items: TransactionEvent[]) {
  const page: TransactionEventPage = { items, total: items.length };
  vi.mocked(listTransactionEvents).mockResolvedValue(page);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <HistoryDrawer transactionId={7} title="Netflix" onClose={() => {}} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("HistoryDrawer learned-from-you badge", () => {
  it("shows the badge on a receipt that taught the learned map", async () => {
    renderDrawer([
      receipt({
        payload: {
          fields: ["category_id"],
          changes: { category_id: 7 },
          category_id: 7,
          learned: { normalized_payee: "NETFLIX", category_id: 7 },
        },
      }),
    ]);

    expect(await screen.findByText("learned from you")).toBeTruthy();
    // The receipt itself is still the headline.
    expect(screen.getByText("you set category Subscriptions")).toBeTruthy();
    expect(screen.getByTitle(`receipt ${HASH}`)).toBeTruthy();
  });

  it("shows no badge on an edit that taught nothing", async () => {
    renderDrawer([
      receipt({
        event_kind: "edited",
        summary: "you edited date",
        payload: { fields: ["date"], changes: { date: "2026-10-05" } },
      }),
    ]);

    await screen.findByText("you edited date");
    expect(screen.queryByText("learned from you")).toBeNull();
  });

  it("treats a present-but-null or corrupt marker as no marker", async () => {
    renderDrawer([
      receipt({ summary: "you edited date", payload: { fields: ["date"], learned: null } }),
      receipt({
        id: 12,
        summary: "you edited note",
        payload: { fields: ["note"], learned: "corrupt" },
      }),
    ]);

    expect(await screen.findAllByText("you edited date")).toHaveLength(1);
    expect(screen.getAllByText("you edited note")).toHaveLength(1);
    expect(screen.queryByText("learned from you")).toBeNull();
  });

  it("survives a receipt with no payload at all (the drawer already treats it as optional)", async () => {
    renderDrawer([
      receipt({ payload: undefined as unknown as Record<string, unknown> }),
    ]);

    const rows = await screen.findAllByText("you set category Subscriptions");
    expect(rows).toHaveLength(1);
    expect(screen.queryByText("learned from you")).toBeNull();
  });

  it("badges every receipt in a batch of corrections", async () => {
    renderDrawer([
      receipt({
        id: 21,
        payload: {
          fields: ["category_id"],
          changes: { category_id: 7 },
          learned: { normalized_payee: "NETFLIX", category_id: 7 },
        },
      }),
      receipt({
        id: 22,
        payload: {
          fields: ["category_id"],
          changes: { category_id: 9 },
          learned: { normalized_payee: "HYDRO ONE", category_id: 9 },
        },
      }),
    ]);

    const badges = await screen.findAllByText("learned from you");
    expect(badges).toHaveLength(2);
  });
});

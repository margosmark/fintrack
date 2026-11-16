// M165 — the two suggestion surfaces must never offer to CREATE a bill that
// already exists (docs/09 §Recurring prompt + §Bills you probably have).
//
// The manufactured duplicate Mark reported: ONE YouTube Premium subscription
// arriving as TWO candidates (the statement spellings split by the space in
// `GOOGLE *YouTubePremium`), both offering "Create bill", for a payee he had
// already turned into a bill. Two halves of that bug live in different
// layers — the merge is a BACKEND property
// (`backend/tests/test_recurring.py::test_statement_spellings_are_one_candidate_group`),
// and this file pins the half the card owns:
//
//   * a covered candidate keeps its row and its Tag / Not-recurring answers,
//     loses the Create-bill CTA, and SAYS why — `covered by '<bill>' → /bills`;
//   * an uncovered candidate still gets its Create-bill hand-off;
//   * the suppression POST still works per group (the card's "no" is durable);
//   * both surfaces — the M96 prompt card on /txns and the M138 subscriptions
//     card on /dashboard — follow the same rule.
import type { ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const toastSpies = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  undo: vi.fn(),
}));
vi.mock("../src/components/ui/Toast", () => ({ useToast: () => toastSpies }));

/** The 6-second Undo window the dashboard card's dismissal waits out. */
interface UndoRequest {
  title: string;
  description?: string;
  durationMs?: number;
  onCommit: () => void;
  onUndo: () => void;
}
const undoRequests: UndoRequest[] = [];
toastSpies.undo.mockImplementation((request: UndoRequest) => {
  undoRequests.push(request);
});

const mocks = vi.hoisted(() => ({
  getRecurringCandidates: vi.fn(),
  acceptRecurringGroup: vi.fn(),
  suppressRecurringGroup: vi.fn(),
  listBills: vi.fn(),
}));

vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  getRecurringCandidates: mocks.getRecurringCandidates,
  acceptRecurringGroup: mocks.acceptRecurringGroup,
  suppressRecurringGroup: mocks.suppressRecurringGroup,
}));

vi.mock("../src/api/bills", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/bills")>()),
  listBills: mocks.listBills,
}));

import type { Bill } from "../src/api/bills";
import { SubscriptionsPanel } from "../src/components/reports/SubscriptionsPanel";
import { RecurringPromptCard } from "../src/components/transactions/RecurringPromptCard";
import type { RecurringCandidate, RecurringCandidatePage } from "../src/api/transactions";
import { clearCandidates } from "../src/lib/recurringSession";

/** One statement spelling — the merged group the detector now answers. */
const YOUTUBE: RecurringCandidate = {
  group_key: "GOOGLE*YOUTUBEPREMIUM|monthly",
  dismiss_key: "GOOGLE*YOUTUBEPREMIUM|monthly",
  payee: "GOOGLE *YouTubePremium",
  cadence: "monthly",
  occurrence_count: 13,
  transaction_ids: [1, 2, 3],
  monthly_total: "-25.98",
  first_date: "2025-09-30",
  last_date: "2026-09-28",
  next_expected_date: "2026-10-28",
};

const UNCOVERED: RecurringCandidate = {
  group_key: "SPREITMANS|monthly",
  dismiss_key: "SPREITMANS|subscriptions",
  payee: "SP REITMANS",
  cadence: "monthly",
  occurrence_count: 3,
  transaction_ids: [4, 5, 6],
  monthly_total: "-321.06",
  first_date: "2026-05-27",
  last_date: "2026-07-28",
  next_expected_date: "2026-08-28",
};

function page(groups: RecurringCandidate[]): RecurringCandidatePage {
  return {
    groups,
    combined_monthly_total: "-25.98",
    suppressed_count: 0,
    window_start: "2025-09-28",
  };
}

/** Bill 66 as Mark's own ledger holds it: two spellings, monthly, enabled. */
function youtubeBill(overrides: Partial<Bill> = {}): Bill {
  return {
    id: 66,
    user_id: 343,
    name: "Google *Youtubepremium",
    amount: "25.98",
    category_id: null,
    frequency: "monthly",
    next_due_date: "2026-09-28",
    remind_days_before: 3,
    enabled: true,
    match_mode: "contains",
    match_term: "Google *Youtubepremium",
    match_terms: ["Google *Youtubepremium", "Google*Youtubepremium"],
    match_logic: "any",
    amount_tolerance: null,
    ...overrides,
  };
}

function mount(node: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/transactions"]}>
      <QueryClientProvider client={client}>{node}</QueryClientProvider>
    </MemoryRouter>,
  );
}

function mountPrompt() {
  return mount(<RecurringPromptCard />);
}

function mountPanel() {
  return mount(<SubscriptionsPanel />);
}

beforeEach(() => {
  vi.clearAllMocks();
  // The prompt card caches its candidates in sessionStorage "once per browser
  // session": a previous test's answered state would hide the card under test.
  clearCandidates();
  undoRequests.length = 0;
  toastSpies.undo.mockImplementation((request: UndoRequest) => {
    undoRequests.push(request);
  });
  // Default: the cards ask for bills and get none — every test that needs
  // coverage opts in explicitly.
  mocks.listBills.mockResolvedValue([]);
  mocks.suppressRecurringGroup.mockResolvedValue(undefined);
});

describe("RecurringPromptCard (M96, /txns)", () => {
  it("one row for the payee a bill already covers: no Create CTA, the covered line instead", async () => {
    mocks.getRecurringCandidates.mockResolvedValue(page([YOUTUBE]));
    mocks.listBills.mockResolvedValue([youtubeBill()]);
    mountPrompt();
    await screen.findByTestId("recurring-prompt");

    // ONE row — the statement spellings arrive merged (pinned server-side).
    expect(screen.getAllByTestId("covered-by-bill")).toHaveLength(1);
    const line = screen.getByTestId("covered-by-bill");
    expect(line.textContent).toContain("covered by 'Google *Youtubepremium' → /bills");
    // The muted line is a LINK to the existing record, never a button.
    expect(screen.queryByRole("button", { name: /create bill/i })).toBeNull();
    expect(screen.getByRole("link", { name: /open the 'Google \*Youtubepremium' bill/i })).toBeTruthy();
    // The row still answers the two questions it always answered.
    expect(screen.getByRole("button", { name: "Tag as recurring" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Not recurring" })).toBeTruthy();
  });

  it("the suppression POST still works for a covered group", async () => {
    mocks.getRecurringCandidates.mockResolvedValue(page([YOUTUBE]));
    mocks.listBills.mockResolvedValue([youtubeBill()]);
    mountPrompt();
    await screen.findByTestId("recurring-prompt");

    fireEvent.click(screen.getByRole("button", { name: "Not recurring" }));
    await waitFor(() =>
      expect(mocks.suppressRecurringGroup).toHaveBeenCalledWith(YOUTUBE.group_key),
    );
    expect(screen.queryByTestId("recurring-prompt")).toBeNull();
  });

  it("a candidate NO bill tracks keeps its Create-bill hand-off", async () => {
    mocks.getRecurringCandidates.mockResolvedValue(page([YOUTUBE, UNCOVERED]));
    mocks.listBills.mockResolvedValue([youtubeBill()]);
    mountPrompt();
    await screen.findByTestId("recurring-prompt");

    expect(screen.getByRole("button", { name: "Create bill reminder for SP REITMANS" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Create bill reminder for GOOGLE/i })).toBeNull();
    expect(screen.getByTestId("covered-by-bill")).toBeTruthy();
    // The covered row costs nothing else: the card's count still lists both.
    expect(screen.getByTestId("recurring-prompt").textContent).toContain("2 candidates");
  });

  it("an unloaded or failed bills read claims NO coverage — unknown is not 'covered'", async () => {
    mocks.getRecurringCandidates.mockResolvedValue(page([YOUTUBE]));
    mocks.listBills.mockRejectedValue(new Error("offline"));
    mountPrompt();
    await screen.findByTestId("recurring-prompt");

    expect(screen.queryByTestId("covered-by-bill")).toBeNull();
    expect(screen.getByRole("button", { name: /Create bill reminder for GOOGLE/i })).toBeTruthy();
  });

  it("a DISABLED bill does not cover anything", async () => {
    mocks.getRecurringCandidates.mockResolvedValue(page([YOUTUBE]));
    mocks.listBills.mockResolvedValue([youtubeBill({ enabled: false })]);
    mountPrompt();
    await screen.findByTestId("recurring-prompt");

    expect(screen.queryByTestId("covered-by-bill")).toBeNull();
    expect(screen.getByRole("button", { name: /Create bill reminder for GOOGLE/i })).toBeTruthy();
  });

  it("a monthly bill covers a 28-day-cycle candidate (cadence tolerance)", async () => {
    mocks.getRecurringCandidates.mockResolvedValue(page([YOUTUBE, { ...YOUTUBE, group_key: "GOOGLE*YOUTUBEPREMIUM|four_weekly", dismiss_key: "GOOGLE*YOUTUBEPREMIUM|four_weekly", payee: "Google*Youtubepremium", cadence: "four_weekly", transaction_ids: [7, 8, 9], occurrence_count: 3 }]));
    mocks.listBills.mockResolvedValue([youtubeBill()]);
    mountPrompt();
    await screen.findByTestId("recurring-prompt");

    // Both rows are monthly streams to the bill: neither may offer to create.
    expect(screen.getAllByTestId("covered-by-bill")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /create bill/i })).toBeNull();
  });
});

describe("SubscriptionsPanel (M138, /dashboard)", () => {
  it("suppresses its Create-bill CTA for a covered candidate and says why", async () => {
    mocks.getRecurringCandidates.mockResolvedValue(page([YOUTUBE]));
    mocks.listBills.mockResolvedValue([youtubeBill()]);
    mountPanel();
    await screen.findByText("Bills you probably have");

    expect(screen.getByTestId("covered-by-bill").textContent).toContain(
      "covered by 'Google *Youtubepremium' → /bills",
    );
    expect(screen.queryByRole("button", { name: /create bill reminder/i })).toBeNull();
    // The dismissal answer is unaffected — a covered row can still be dropped.
    expect(screen.getByRole("button", { name: /not a bill/i })).toBeTruthy();
  });

  it("keeps the Create-bill CTA when no bill tracks the payee", async () => {
    mocks.getRecurringCandidates.mockResolvedValue(page([UNCOVERED]));
    mocks.listBills.mockResolvedValue([youtubeBill()]);
    mountPanel();
    await screen.findByText("Bills you probably have");

    expect(screen.queryByTestId("covered-by-bill")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Create bill reminder from SP REITMANS" }),
    ).toBeTruthy();
  });
});

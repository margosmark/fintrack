// M138 — the dashboard subscriptions card (docs/09 §Bills you probably have).
//
// The M133 evidence was a card with three failures at once: a 2-occurrence
// detector surfacing FILM.CA / LOBLAW / TIM HORTONS / SHELL as bill
// suggestions, no dismissal of any kind, and an empty state that claimed
// "No subscriptions detected". All three are pinned here:
//
//   * the substrate is the STRICT detector (`?source=subscriptions`), so the
//     rows are `RecurringCandidate`s and carry a `dismiss_key`;
//   * cap-5 + "Show all", per-row "Not a bill", header "Dismiss all (N)";
//   * dismissal is a 6-second Undo toast — NOTHING is POSTed while the window
//     is open, and Undo is a pure local restore (no unsuppress route exists);
//   * the empty state never claims nothing was detected after a dismissal;
//   * the card's total counts money going OUT (M133 D-11's sign complaint).
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SubscriptionsPanel } from "../src/components/reports/SubscriptionsPanel";
import type { RecurringCandidate, RecurringCandidatePage } from "../src/api/transactions";

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  undo: vi.fn(),
}));

vi.mock("../src/components/ui/Toast", () => ({ useToast: () => toast }));

vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  getRecurringCandidates: vi.fn(),
  suppressRecurringGroup: vi.fn(),
}));

// M165: the panel reads the `["bills"]` cache to decide whether a suggestion
// is already a bill — resolved EMPTY here so these substrate tests keep
// asserting the uncovered behaviour they own (coverage has its own file,
// `tests/m165BillCoverage.test.tsx`).
vi.mock("../src/api/bills", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/bills")>()),
  listBills: vi.fn(),
}));

import { listBills } from "../src/api/bills";
import { getRecurringCandidates, suppressRecurringGroup } from "../src/api/transactions";

interface UndoRequest {
  title: string;
  description?: string;
  durationMs?: number;
  onCommit: () => void;
  onUndo: () => void;
}

/** Every `toast.undo(...)` the panel has asked for, in order. */
let undoRequests: UndoRequest[] = [];

function candidate(index: number, overrides: Partial<RecurringCandidate> = {}): RecurringCandidate {
  const payee = overrides.payee ?? `MERCHANT ${index}`;
  return {
    group_key: `${payee}|monthly`,
    dismiss_key: `${payee}|subscriptions`,
    payee,
    cadence: "monthly",
    occurrence_count: 4,
    transaction_ids: [index, index + 100, index + 200, index + 300],
    monthly_total: "-10.00",
    first_date: "2026-06-01",
    last_date: "2026-09-01",
    next_expected_date: "2026-10-01",
    ...overrides,
  };
}

function page(groups: RecurringCandidate[]): RecurringCandidatePage {
  return {
    groups,
    combined_monthly_total: "-10.00",
    suppressed_count: 0,
    window_start: "2025-09-01",
  };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <QueryClientProvider client={client}>
        <SubscriptionsPanel />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  undoRequests = [];
  toast.undo.mockReset();
  toast.error.mockReset();
  toast.undo.mockImplementation((request: UndoRequest) => {
    undoRequests.push(request);
  });
  vi.mocked(suppressRecurringGroup).mockResolvedValue(undefined);
  vi.mocked(listBills).mockResolvedValue([]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("M138 substrate — the strict detector, not the M27 shim", () => {
  it("asks the server for the subscriptions source", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(page([candidate(1)]));
    renderPanel();
    expect(await screen.findByText("MERCHANT 1")).toBeTruthy();
    expect(vi.mocked(getRecurringCandidates)).toHaveBeenCalledWith("subscriptions");
  });

  it("names the rule it applies, not the rule that produced the junk", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(page([candidate(1)]));
    renderPanel();
    await screen.findByText("Bills you probably have");
    // The old hint was literally the bug: "2 or more charges".
    expect(document.body.textContent).not.toContain("2 or more charges");
  });

  it("counts only money going out in the card total", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(
      page([
        candidate(1, { payee: "PAYROLL", monthly_total: "5000.00" }),
        candidate(2, { payee: "NETFLIX", monthly_total: "-15.99" }),
      ]),
    );
    renderPanel();
    // PAYROLL clears the strict rule and keeps its row (M133's strict six),
    // but a credit must never inflate a figure headed "Bills" (D-11).
    expect(await screen.findByText("PAYROLL")).toBeTruthy();
    expect(screen.getByText("$15.99/mo")).toBeTruthy();
    expect(screen.getByText("($191.88/yr)")).toBeTruthy();
  });
});

describe("cap + Show all (M133 D-4)", () => {
  it("shows 5 rows and reveals the rest through Show all", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(
      page(Array.from({ length: 7 }, (_, index) => candidate(index + 1))),
    );
    renderPanel();

    expect(await screen.findAllByRole("button", { name: /^Not a bill/ })).toHaveLength(5);
    fireEvent.click(screen.getByRole("button", { name: "Show all (7)" }));
    expect(screen.getAllByRole("button", { name: /^Not a bill/ })).toHaveLength(7);
    fireEvent.click(screen.getByRole("button", { name: "Show fewer" }));
    expect(screen.getAllByRole("button", { name: /^Not a bill/ })).toHaveLength(5);
  });

  it("offers no Show all when the card is already under the cap", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(
      page([candidate(1), candidate(2)]),
    );
    renderPanel();
    expect(await screen.findAllByRole("button", { name: /^Not a bill/ })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /^Show all/ })).toBeNull();
  });
});

describe("dismissal (M133 D-3 — one vocabulary, deferred write)", () => {
  it("hides a row on 'Not a bill' and writes nothing while Undo is up", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(
      page([candidate(1), candidate(2)]),
    );
    renderPanel();
    expect(await screen.findAllByRole("button", { name: /^Not a bill/ })).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Not a bill — MERCHANT 1" }));

    expect(screen.queryByText("MERCHANT 1")).toBeNull();
    expect(screen.getByText("MERCHANT 2")).toBeTruthy();
    expect(vi.mocked(suppressRecurringGroup)).not.toHaveBeenCalled();

    // The undo toast carries the M138 window, not M95's 5s.
    expect(undoRequests).toHaveLength(1);
    expect(undoRequests[0].durationMs).toBe(6000);
    expect(undoRequests[0].title).toBe("Dismissed MERCHANT 1");

    undoRequests[0].onUndo();
    expect(await screen.findByText("MERCHANT 1")).toBeTruthy();
    expect(vi.mocked(suppressRecurringGroup)).not.toHaveBeenCalled();
  });

  it("persists every key exactly once when the window closes", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(
      page([candidate(1), candidate(2), candidate(3)]),
    );
    renderPanel();
    expect(await screen.findAllByRole("button", { name: /^Not a bill/ })).toHaveLength(3);

    fireEvent.click(screen.getByRole("button", { name: "Dismiss all 3 suggestions" }));
    expect(await screen.findByText("Dismissed 3 suggestions")).toBeTruthy();
    expect(vi.mocked(suppressRecurringGroup)).not.toHaveBeenCalled();

    undoRequests[0].onCommit();
    await waitFor(() => expect(vi.mocked(suppressRecurringGroup)).toHaveBeenCalledTimes(3));
    expect(vi.mocked(suppressRecurringGroup).mock.calls.map(([key]) => key)).toEqual([
      "MERCHANT 1|subscriptions",
      "MERCHANT 2|subscriptions",
      "MERCHANT 3|subscriptions",
    ]);
    // ...and the rows stay gone across the refetch the commit triggers.
    expect(screen.queryByText("MERCHANT 1")).toBeNull();
    expect(screen.getByText("Dismissed 3 suggestions")).toBeTruthy();
  });

  it("restores the rows and explains itself when the write fails", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(
      page([candidate(1), candidate(2)]),
    );
    vi.mocked(suppressRecurringGroup).mockRejectedValueOnce(new Error("boom"));
    renderPanel();
    expect(await screen.findAllByRole("button", { name: /^Not a bill/ })).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Not a bill — MERCHANT 1" }));
    undoRequests[0].onCommit();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Could not dismiss these suggestions",
        expect.anything(),
      ),
    );
    expect(await screen.findByText("MERCHANT 1")).toBeTruthy();
  });
});

describe("empty states (M133 dismissal_design)", () => {
  it("never claims nothing was detected after a dismissal", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(page([candidate(1)]));
    renderPanel();
    expect(await screen.findByText("MERCHANT 1")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Not a bill — MERCHANT 1" }));
    undoRequests[0].onCommit();

    expect(await screen.findByText("Dismissed 1 suggestion")).toBeTruthy();
    expect(screen.queryByText("No subscriptions detected")).toBeNull();
    expect(document.body.textContent).not.toContain("No subscriptions detected");
  });

  it("says so honestly when the strict rule simply matched nothing", async () => {
    // Case 2 of the bills-state table (t_cc3b35f1): candidates empty AND
    // zero bills defined (listBills resolves [] in beforeEach) → the notice
    // renders, copy unchanged.
    vi.mocked(getRecurringCandidates).mockResolvedValue(page([]));
    renderPanel();
    expect(await screen.findByText("No bill suggestions right now")).toBeTruthy();
    expect(document.body.textContent).not.toContain("No subscriptions detected");
  });

  // Case 1: bills are already defined — the notice is onboarding advice the
  // user has taken, so the card renders NOTHING. Absence is the only
  // statement: no title, no hint, no preview, no CTA, and never a claim
  // about the user's bills.
  it("renders nothing at all when the user already has bills", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(page([]));
    vi.mocked(listBills).mockResolvedValue([{ id: "b1" }] as never);
    const { container } = renderPanel();

    await waitFor(() => expect(container.firstChild).toBeNull());
    expect(screen.queryByText("No bill suggestions right now")).toBeNull();
    expect(screen.queryByRole("link", { name: "Import a statement" })).toBeNull();
    expect(document.body.textContent).not.toContain("No bill suggestions right now");
  });

  // Case 3: candidates empty but the shared ["bills"] query has not settled —
  // the SAME skeleton as the candidates-loading branch, so the notice never
  // flashes then vanishes. The promise never resolves, so this is a stable
  // state, not a race.
  it("holds the skeleton, never the notice, while the bills query is still pending", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(page([]));
    vi.mocked(listBills).mockImplementation(() => new Promise(() => {}));
    renderPanel();

    await waitFor(() => expect(vi.mocked(getRecurringCandidates)).toHaveBeenCalled());
    // Let the candidates resolution land (act flushes the query's state
    // update); the bills promise above never settles, so the panel must be
    // parked on the bills-pending skeleton from here on.
    await vi.mocked(getRecurringCandidates).mock.results[0].value;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(screen.getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(screen.queryByText("No bill suggestions right now")).toBeNull();
    expect(screen.queryByRole("link", { name: "Import a statement" })).toBeNull();
  });

  // Case 4: an ERRORED bills query is unknown, and unknown must never be
  // read as "has bills" — the notice still renders.
  it("keeps the notice when the bills query errors", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(page([]));
    vi.mocked(listBills).mockRejectedValue(new Error("offline"));
    renderPanel();
    expect(await screen.findByText("No bill suggestions right now")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Import a statement" })).toBeTruthy();
  });

  it("points at the Bills page rather than a management screen that does not exist", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(page([candidate(1)]));
    renderPanel();
    expect(await screen.findByText("MERCHANT 1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Not a bill — MERCHANT 1" }));
    undoRequests[0].onCommit();
    expect(await screen.findByRole("link", { name: "Go to Bills" })).toBeTruthy();
  });
});

// M96 B2 — the recurring-transactions prompt card (docs/09 §Recurring prompt).
//
// What is pinned here is the part the spec calls "final":
//   * the card renders the candidates, the combined monthly total and the
//     privacy sentence VERBATIM (it is a claim about where data goes, so the
//     copy is test-pinned, not vibes);
//   * ACCEPT tags exactly the rows it showed and invalidates the rollups the
//     badge and Budgets read;
//   * DECLINE (per group or card-wide) writes the suppression and removes the
//     group from THIS SESSION'S cache, so a remount/refresh never re-asks —
//     "prompt not recomputed per load" is asserted by counting network calls;
//   * a failed scan hides the card instead of nagging, and caches nothing, so
//     the next entry retries.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const toastSpies = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  undo: vi.fn(),
}));

vi.mock("../src/components/ui/Toast", () => ({ useToast: () => toastSpies }));

/** M138: the card's bulk dismissal is a 6-second Undo toast now (docs/09). */
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

vi.mock("../src/api/transactions", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/api/transactions")>();
  return {
    ...mod,
    getRecurringCandidates: vi.fn(),
    acceptRecurringGroup: vi.fn(),
    suppressRecurringGroup: vi.fn(),
  };
});

// M165: the card reads the `["bills"]` cache to decide whether a candidate is
// already a bill — resolved EMPTY so these tests keep pinning the uncovered
// affordances they own (the covered rendering lives in
// `tests/m165BillCoverage.test.tsx`).
vi.mock("../src/api/bills", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/bills")>()),
  listBills: vi.fn(),
}));

import { listBills } from "../src/api/bills";
import {
  acceptRecurringGroup,
  getRecurringCandidates,
  suppressRecurringGroup,
} from "../src/api/transactions";
import type { RecurringCandidate, RecurringCandidatePage } from "../src/api/transactions";
import {
  RECURRING_PRIVACY_COPY,
  RecurringPromptCard,
} from "../src/components/transactions/RecurringPromptCard";
import { committedMonthly, recurringBadgeTitle } from "../src/lib/recurring";
import { clearCandidates, readCandidates, writeCandidates } from "../src/lib/recurringSession";

function candidate(overrides: Partial<RecurringCandidate> = {}): RecurringCandidate {
  return {
    group_key: "NETFLIX|monthly",
    dismiss_key: "NETFLIX|monthly",
    payee: "Netflix",
    cadence: "monthly",
    occurrence_count: 4,
    transaction_ids: [11, 12, 13, 14],
    monthly_total: "-15.99",
    first_date: "2026-06-03",
    last_date: "2026-09-03",
    next_expected_date: "2026-10-03",
    ...overrides,
  };
}

const NETFLIX = candidate();
const HYDRO = candidate({
  group_key: "HYDRO|monthly",
  dismiss_key: "HYDRO|monthly",
  payee: "Hydro One",
  transaction_ids: [21, 22, 23],
  monthly_total: "-6.00",
});

const PAGE: RecurringCandidatePage = {
  groups: [NETFLIX, HYDRO],
  combined_monthly_total: "-21.99",
  suppressed_count: 1,
  window_start: "2025-09-28",
};

/** Always-rendered location probe: the card's "Create bill" hand-off is a NAVIGATION. */
function RouteProbe() {
  const location = useLocation();
  return <div data-testid="route-probe">{`${location.pathname}${location.search}`}</div>;
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  // M116: the card now offers a hand-off into /bills, so it needs a router —
  // the same harness fix M55 made for `SubscriptionsPanel`.
  const view = render(
    <MemoryRouter initialEntries={["/transactions"]}>
      <QueryClientProvider client={client}>
        <RouteProbe />
        <RecurringPromptCard />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...view, invalidate };
}

beforeEach(() => {
  vi.clearAllMocks();
  undoRequests.length = 0;
  toastSpies.undo.mockImplementation((request: UndoRequest) => {
    undoRequests.push(request);
  });
  clearCandidates();
  vi.mocked(listBills).mockResolvedValue([]);
});

afterEach(() => {
  clearCandidates();
});

describe("RecurringPromptCard", () => {
  it("lists the candidates, the combined monthly total, and the privacy line verbatim", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(PAGE);
    mount();
    const card = await screen.findByTestId("recurring-prompt");

    expect(
      screen.getByRole("heading", { name: "These look recurring — tag them?" }),
    ).toBeTruthy();
    // Two groups → "2 candidates", and the server's combined number.
    expect(card.textContent).toContain("2 candidates");
    expect(card.textContent).toContain("-$21.99");
    expect(card.textContent).toContain("Netflix");
    expect(card.textContent).toContain("Hydro One");
    expect(card.textContent).toContain("Monthly");
    expect(card.textContent).toContain("next 2026-10-03");
    // The docs/09 privacy claim, byte for byte.
    expect(card.textContent).toContain(RECURRING_PRIVACY_COPY);
  });

  it("M116: 'Create bill' hands the group to a prefilled /bills draft", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(PAGE);
    mount();
    fireEvent.click(
      await screen.findByRole("button", { name: "Create bill reminder for Netflix" }),
    );
    const probe = screen.getByTestId("route-probe").textContent ?? "";
    expect(probe.startsWith("/bills?")).toBe(true);
    const params = new URLSearchParams(probe.slice(probe.indexOf("?")));
    expect(params.get("autofill")).toBe("Netflix");
    expect(params.get("match")).toBe("Netflix");
    expect(params.get("frequency")).toBe("monthly");
    expect(params.get("due")).toBe("2026-10-03");
    // A bill's `amount` is what it COSTS: the detector's SIGNED monthly total
    // must not reach the amount band (|tx| vs amount), or it matches nothing.
    expect(params.get("amount")).toBe("15.99");
    // Hand-off only — nothing is written until the form is submitted (M55 d6).
    expect(toastSpies.success).not.toHaveBeenCalled();
  });

  it("M116: a group with no bill cadence says so and stays put", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue({
      ...PAGE,
      groups: [candidate({ cadence: "weekly" })],
    });
    mount();
    fireEvent.click(
      await screen.findByRole("button", { name: /create bill reminder/i }),
    );
    expect(screen.getByTestId("route-probe").textContent).toBe("/transactions");
    expect(toastSpies.info).toHaveBeenCalledTimes(1);
  });

  it("accept tags exactly the rows it showed and invalidates the rollup caches", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(PAGE);
    vi.mocked(acceptRecurringGroup).mockResolvedValue({
      updated: 4,
      group_key: NETFLIX.group_key,
      cadence: "monthly",
      next_expected_date: "2026-10-03",
    });
    const { invalidate } = mount();
    await screen.findByTestId("recurring-prompt");

    fireEvent.click(screen.getAllByRole("button", { name: "Tag as recurring" })[0]);
    await waitFor(() =>
      expect(vi.mocked(acceptRecurringGroup)).toHaveBeenCalledWith({
        group_key: NETFLIX.group_key,
        transaction_ids: NETFLIX.transaction_ids,
        cadence: "monthly",
      }),
    );
    // Badge + Budgets + Reports readers all re-read.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["transactions"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["budgets"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["reports"] });

    // Per-group, never all-or-nothing: the other candidate is still offered.
    expect(screen.queryByText("Netflix")).toBeNull();
    expect(screen.getByText("Hydro One")).toBeTruthy();
    expect(readCandidates()?.map((group) => group.group_key)).toEqual([HYDRO.group_key]);
  });

  it("declines suppress the group, and a decline is never asked again", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(PAGE);
    vi.mocked(suppressRecurringGroup).mockResolvedValue(undefined);
    mount();
    await screen.findByTestId("recurring-prompt");

    fireEvent.click(screen.getAllByRole("button", { name: "Not recurring" })[0]);
    await waitFor(() =>
      expect(vi.mocked(suppressRecurringGroup)).toHaveBeenCalledWith(NETFLIX.group_key),
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Not recurring" })[0]);
    await waitFor(() => expect(readCandidates()).toEqual([]));
    expect(screen.queryByTestId("recurring-prompt")).toBeNull();

    // Re-entering the page (fresh mount = fresh query cache) must NOT scan
    // again: the session cache holds the answered state.
    const calls = vi.mocked(getRecurringCandidates).mock.calls.length;
    mount();
    expect(vi.mocked(getRecurringCandidates)).toHaveBeenCalledTimes(calls);
    expect(screen.queryByTestId("recurring-prompt")).toBeNull();
  });

  it("computes the scan ONCE per session — a reload reads the cache", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(PAGE);
    const first = mount();
    await screen.findByTestId("recurring-prompt");
    first.unmount();

    // New mount with a brand-new QueryClient: the in-memory cache is gone,
    // so any second request would prove the scan runs per page-load.
    mount();
    await screen.findByTestId("recurring-prompt");
    expect(vi.mocked(getRecurringCandidates)).toHaveBeenCalledTimes(1);
  });

  it("card-level dismiss hides everything at once and writes only after the Undo window", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(PAGE);
    vi.mocked(suppressRecurringGroup).mockResolvedValue(undefined);
    mount();
    await screen.findByTestId("recurring-prompt");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss all 2 suggestions for good" }));

    // Hidden immediately, nothing sent yet, session cache already emptied —
    // so a remount inside the window cannot re-ask (M96's "prompt not
    // recomputed per load" rule still holds).
    expect(screen.queryByTestId("recurring-prompt")).toBeNull();
    expect(vi.mocked(suppressRecurringGroup)).not.toHaveBeenCalled();
    expect(readCandidates()).toEqual([]);
    expect(undoRequests).toHaveLength(1);
    expect(undoRequests[0].durationMs).toBe(6000);

    // Stage 2: the REAL requests, once the window closes.
    undoRequests[0].onCommit();
    await waitFor(() => expect(vi.mocked(suppressRecurringGroup)).toHaveBeenCalledTimes(2));
    expect(vi.mocked(suppressRecurringGroup)).toHaveBeenCalledWith(NETFLIX.group_key);
    expect(vi.mocked(suppressRecurringGroup)).toHaveBeenCalledWith(HYDRO.group_key);
    expect(screen.queryByTestId("recurring-prompt")).toBeNull();
    expect(readCandidates()).toEqual([]);
  });

  it("Undo during the window restores the cache and sends nothing", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue(PAGE);
    vi.mocked(suppressRecurringGroup).mockResolvedValue(undefined);
    mount();
    await screen.findByTestId("recurring-prompt");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss all 2 suggestions for good" }));
    undoRequests[0].onUndo();

    expect(await screen.findByTestId("recurring-prompt")).toBeTruthy();
    expect(readCandidates()).toHaveLength(2);
    expect(vi.mocked(suppressRecurringGroup)).not.toHaveBeenCalled();
  });

  it("a failed scan hides the card and caches nothing (so the next entry retries)", async () => {
    vi.mocked(getRecurringCandidates).mockRejectedValue(new Error("boom"));
    mount();
    await waitFor(() => expect(vi.mocked(getRecurringCandidates)).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("recurring-prompt")).toBeNull();
    expect(readCandidates()).toBeNull();
    expect(toastSpies.error).not.toHaveBeenCalled();
  });

  it("shows nothing when the server answers with no candidates", async () => {
    vi.mocked(getRecurringCandidates).mockResolvedValue({
      ...PAGE,
      groups: [],
      combined_monthly_total: "0.00",
    });
    mount();
    await waitFor(() => expect(vi.mocked(getRecurringCandidates)).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("recurring-prompt")).toBeNull();
    // Empty IS an answer: cached, so the card does not re-scan this session.
    expect(readCandidates()).toEqual([]);
  });
});

describe("committedMonthly — the Budgets label (docs/09: label, don't recompute)", () => {
  const row = (overrides: Partial<Parameters<typeof committedMonthly>[0][number]> = {}) =>
    ({
      id: 1,
      currency: "CAD",
      amount_native: "-100.00",
      cad_amount: "-100.00",
      is_recurring: true,
      recurring_cadence: "monthly",
      recurring_next_date: "2026-11-03",
      ...overrides,
    }) as Parameters<typeof committedMonthly>[0][number];

  it("is zero with nothing tagged", () => {
    expect(committedMonthly([])).toEqual({ count: 0, monthly: 0 });
    expect(committedMonthly([row({ is_recurring: false })])).toEqual({ count: 0, monthly: 0 });
  });

  it("keeps a monthly row at face value and normalises a weekly one", () => {
    expect(committedMonthly([row()])).toEqual({ count: 1, monthly: -100 });
    expect(committedMonthly([row({ recurring_cadence: "weekly" })])).toEqual({
      count: 1,
      monthly: -434.82, // 100 × 30.4375/7
    });
  });

  it("counts foreign rows at their CAD mirror and EXCLUDES rows without one", () => {
    expect(
      committedMonthly([
        row({ currency: "USD", amount_native: "-50.00", cad_amount: "-68.00" }),
      ]),
    ).toEqual({ count: 1, monthly: -68 });
    expect(
      committedMonthly([row({ currency: "USD", amount_native: "-50.00", cad_amount: null })]),
    ).toEqual({ count: 0, monthly: 0 });
  });

  it("builds a neutral badge title with the next expected date", () => {
    expect(recurringBadgeTitle("monthly", "2026-11-03")).toBe("Monthly · next 2026-11-03");
    expect(recurringBadgeTitle("four_weekly", null)).toBe("Every 4 weeks");
    expect(recurringBadgeTitle(null, null)).toBe("Recurring");
  });

  it("writes the session cache and reads it back (and survives a blocked store)", () => {
    writeCandidates([NETFLIX]);
    expect(readCandidates()).toEqual([NETFLIX]);
    clearCandidates();
    expect(readCandidates()).toBeNull();
    // A hostile store must degrade to 'recompute', never throw.
    const original = window.sessionStorage;
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: {
        getItem: () => {
          throw new Error("blocked");
        },
        setItem: () => {
          throw new Error("blocked");
        },
        removeItem: () => {
          throw new Error("blocked");
        },
      },
    });
    expect(readCandidates()).toBeNull();
    expect(() => writeCandidates([NETFLIX])).not.toThrow();
    expect(() => clearCandidates()).not.toThrow();
    Object.defineProperty(window, "sessionStorage", { configurable: true, value: original });
  });
});

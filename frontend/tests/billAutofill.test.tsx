// M55 — subscription → bill draft autofill (docs/25).
//
// Three channels: the pure cadence/param contract, the subscriptions-surface
// hand-off (navigate vs. refuse), and the Bills page wiring (prefilled form,
// `from_subscription_id` on create, never on edit).
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  billAutofillParams,
  billFrequencyFor,
  readBillAutofill,
} from "../src/api/bills";
import { SubscriptionsPanel } from "../src/components/reports/SubscriptionsPanel";
import type { RecurringCandidate } from "../src/api/transactions";
import { ToastProvider } from "../src/components/ui/Toast";
import { Bills } from "../src/pages/Bills";

const mocks = vi.hoisted(() => ({
  getRecurringCandidates: vi.fn(),
  suppressRecurringGroup: vi.fn(),
  listBills: vi.fn(),
  suggestTerms: vi.fn(),
  createBill: vi.fn(),
  updateBill: vi.fn(),
  updateBillRule: vi.fn(),
  deleteBill: vi.fn(),
  markPaid: vi.fn(),
}));

vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  getRecurringCandidates: mocks.getRecurringCandidates,
  suppressRecurringGroup: mocks.suppressRecurringGroup,
}));

vi.mock("../src/api/bills", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/bills")>();
  return {
    ...actual,
    listBills: mocks.listBills,
    // M122: the form's suggestion hook — resolved empty so the autofill
    // tests never reach for the network.
    suggestTerms: mocks.suggestTerms,
    createBill: mocks.createBill,
    updateBill: mocks.updateBill,
    deleteBill: mocks.deleteBill,
    markPaid: mocks.markPaid,
    updateBillRule: mocks.updateBillRule,
  };
});

interface DetectedSub {
  payee: string;
  frequency: string;
  avg_amount: string;
  annual_cost: string;
  occurrences: number;
  last_date: string;
  next_expected: string | null;
}

function detected(overrides: Partial<DetectedSub> = {}): DetectedSub {
  return {
    payee: "NETFLIX.COM",
    frequency: "monthly",
    avg_amount: "15.99",
    annual_cost: "191.88",
    occurrences: 3,
    last_date: "2026-09-17",
    next_expected: "2026-10-17",
    ...overrides,
  };
}

/**
 * M138 (docs/09 §Bills you probably have): the panel is fed by the STRICT
 * detector now, so its rows are `RecurringCandidate`s, not `Subscription`s.
 */
function candidate(overrides: Partial<RecurringCandidate> = {}): RecurringCandidate {
  return {
    group_key: "NETFLIX.COM|monthly",
    dismiss_key: "NETFLIX.COM|subscriptions",
    payee: "NETFLIX.COM",
    cadence: "monthly",
    occurrence_count: 3,
    transaction_ids: [1, 2, 3],
    monthly_total: "15.99",
    first_date: "2026-07-17",
    last_date: "2026-09-17",
    next_expected_date: "2026-10-17",
    ...overrides,
  };
}

function candidatePage(groups: RecurringCandidate[]) {
  return {
    groups,
    combined_monthly_total: "-15.99",
    suppressed_count: 0,
    window_start: "2025-09-01",
  };
}

/** Always-rendered location probe, so navigation (and its absence) is observable. */
function RouteProbe() {
  const location = useLocation();
  return <div data-testid="route-probe">{`${location.pathname}${location.search}`}</div>;
}

function renderWithProviders(ui: JSX.Element, initialEntries: string[] = ["/dashboard"]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <RouteProbe />
          <Routes>
            <Route path="/dashboard" element={ui} />
            <Route path="/bills" element={<Bills />} />
          </Routes>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.listBills.mockResolvedValue([]);
  mocks.suggestTerms.mockResolvedValue([]);
  mocks.createBill.mockResolvedValue({ id: 1 });
  mocks.updateBill.mockResolvedValue({ id: 1 });
  mocks.deleteBill.mockResolvedValue(undefined);
  mocks.markPaid.mockResolvedValue({ id: 1 });
  mocks.updateBillRule.mockResolvedValue({ id: 7 });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("cadence mapping (docs/25 M55 decision 9)", () => {
  it("maps every bill-equivalent detected cadence onto itself", () => {
    expect(billFrequencyFor("monthly")).toBe("monthly");
    expect(billFrequencyFor("quarterly")).toBe("quarterly");
    expect(billFrequencyFor("yearly")).toBe("yearly");
  });

  it("refuses cadences with no bill equivalent — never a silent fallback", () => {
    expect(billFrequencyFor("weekly")).toBeNull();
    expect(billFrequencyFor("biweekly")).toBeNull();
    expect(billFrequencyFor("unknown")).toBeNull();
    expect(billFrequencyFor("one_time")).toBeNull();
  });
});

describe("hand-off query (docs/25 M55 Frontend)", () => {
  it("writes all four documented params for a bill-equivalent subscription", () => {
    const params = billAutofillParams(detected());
    expect(params).not.toBeNull();
    expect(params!.toString()).toBe(
      "autofill=NETFLIX.COM&amount=15.99&frequency=monthly&due=2026-10-17",
    );
  });

  it("produces no hand-off at all for weekly/biweekly detections", () => {
    expect(billAutofillParams(detected({ frequency: "weekly" }))).toBeNull();
    expect(billAutofillParams(detected({ frequency: "biweekly" }))).toBeNull();
  });

  it("round-trips through the parser", () => {
    const draft = readBillAutofill(billAutofillParams(detected())!);
    expect(draft).toEqual({
      fromSubscriptionId: "NETFLIX.COM",
      name: "NETFLIX.COM",
      amount: "15.99",
      frequency: "monthly",
      dueDate: "2026-10-17",
      // M116: no explicit `match` param → the payee IS the match term.
      matchTerm: "NETFLIX.COM",
    });
  });

  it("still hands off when the detector projects no next date", () => {
    const draft = readBillAutofill(billAutofillParams(detected({ next_expected: null }))!);
    expect(draft?.fromSubscriptionId).toBe("NETFLIX.COM");
    expect(draft?.dueDate).toBe("");
  });
});

describe("param parsing degrades instead of producing a bad payload", () => {
  it("returns null without the autofill param", () => {
    expect(readBillAutofill(new URLSearchParams(""))).toBeNull();
    expect(readBillAutofill(new URLSearchParams("amount=15.99&due=2026-10-17"))).toBeNull();
    expect(readBillAutofill(new URLSearchParams("autofill="))).toBeNull();
  });

  it("falls back on a non-numeric amount, an unknown frequency and a bad date", () => {
    const draft = readBillAutofill(
      new URLSearchParams("autofill=SPOT&amount=abc&frequency=fortnightly&due=17/10/2026"),
    );
    expect(draft).toEqual({
      fromSubscriptionId: "SPOT",
      name: "SPOT",
      amount: "",
      frequency: "monthly",
      dueDate: "",
      matchTerm: "SPOT",
    });
  });

  it("keeps a legitimate bill frequency it did not write itself", () => {
    const draft = readBillAutofill(
      new URLSearchParams("autofill=SPOT&frequency=one_time&due=2026-12-31"),
    );
    expect(draft?.frequency).toBe("one_time");
    expect(draft?.dueDate).toBe("2026-12-31");
  });
});

describe("subscriptions surface hand-off (docs/25 M55 Frontend)", () => {
  function renderPanel(groups: RecurringCandidate[]) {
    mocks.getRecurringCandidates.mockResolvedValue(candidatePage(groups) as never);
    return renderWithProviders(<SubscriptionsPanel />);
  }

  it("offers the action on every detected candidate", async () => {
    renderPanel([
      candidate(),
      candidate({
        group_key: "SPOTIFY|weekly",
        dismiss_key: "SPOTIFY|subscriptions",
        payee: "SPOTIFY",
        cadence: "weekly",
        monthly_total: "-5.99",
      }),
    ]);
    const buttons = await screen.findAllByRole("button", { name: /create bill reminder/i });
    expect(buttons).toHaveLength(2);
  });

  it("hands a monthly candidate to a prefilled /bills draft", async () => {
    renderPanel([candidate()]);
    fireEvent.click(await screen.findByRole("button", { name: /create bill reminder/i }));
    expect(screen.getByTestId("route-probe").textContent).toBe(
      "/bills?autofill=NETFLIX.COM&amount=15.99&frequency=monthly&due=2026-10-17",
    );
    // The page the user lands on is actually prefilled…
    expect(await screen.findByPlaceholderText("Bill name")).toHaveProperty(
      "value",
      "NETFLIX.COM",
    );
    // …and nothing was created on the way there.
    expect(mocks.createBill).not.toHaveBeenCalled();
  });

  it("refuses a weekly candidate: no navigation, nothing written", async () => {
    renderPanel([candidate({ group_key: "NETFLIX.COM|weekly", cadence: "weekly" })]);
    fireEvent.click(await screen.findByRole("button", { name: /create bill reminder/i }));
    expect(screen.getByTestId("route-probe").textContent).toBe("/dashboard");
    expect(await screen.findByText(/can't become a bill reminder/i)).toBeTruthy();
    expect(mocks.createBill).not.toHaveBeenCalled();
  });
});

describe("Bills page wiring (docs/25 M55 Frontend)", () => {
  const HANDOFF = "/bills?autofill=NETFLIX.COM&amount=15.99&frequency=monthly&due=2026-10-18";

  it("opens the create form prefilled from the hand-off params", async () => {
    renderWithProviders(<Bills />, [HANDOFF]);
    expect(await screen.findByPlaceholderText("Bill name")).toBeTruthy();
    expect(screen.getByPlaceholderText("Bill name")).toHaveProperty("value", "NETFLIX.COM");
    expect(screen.getByPlaceholderText("Amount (optional)")).toHaveProperty("value", "15.99");
    expect(screen.getByRole("combobox", { name: "Frequency" })).toHaveProperty(
      "value",
      "monthly",
    );
    // M116/M121: the hand-off ALSO prefills the rule with the payee — as the
    // first CHIP of the term list (the input itself starts empty).
    const terms = screen.getByRole("list", { name: "Match terms" });
    expect(within(terms).getByText("NETFLIX.COM")).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "Match terms" })).toHaveProperty("value", "");
    expect(screen.getByDisplayValue("2026-10-18")).toBeTruthy();
  });

  it("sends from_subscription_id on create", async () => {
    renderWithProviders(<Bills />, [HANDOFF]);
    fireEvent.click(await screen.findByRole("button", { name: "Create" }));
    await waitFor(() => expect(mocks.createBill).toHaveBeenCalledTimes(1));
    expect(mocks.createBill).toHaveBeenCalledWith({
      name: "NETFLIX.COM",
      amount: 15.99,
      frequency: "monthly",
      next_due_date: "2026-10-18",
      from_subscription_id: "NETFLIX.COM",
      match_mode: "contains",
      // M121: the rule travels as a TERM LIST with its AND/OR mode; the
      // pre-M121 `match_term` field is the server's own back-compat shim.
      match_terms: ["NETFLIX.COM"],
      match_logic: "any",
      amount_tolerance: undefined,
    });
  });

  it("keeps an edited amount and still carries the subscription id", async () => {
    renderWithProviders(<Bills />, [HANDOFF]);
    const amount = await screen.findByPlaceholderText("Amount (optional)");
    fireEvent.change(amount, { target: { value: "18.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(mocks.createBill).toHaveBeenCalledTimes(1));
    expect(mocks.createBill).toHaveBeenCalledWith(
      expect.objectContaining({ amount: 18.5, from_subscription_id: "NETFLIX.COM" }),
    );
  });

  it("falls back to the ordinary empty form when the params are garbage", async () => {
    renderWithProviders(<Bills />, ["/bills?amount=15.99&due=nope"]);
    expect(await screen.findByRole("button", { name: /new bill/i })).toBeTruthy();
    // Nothing pre-opened and nothing preselected: the user starts from scratch.
    expect(screen.queryByPlaceholderText("Bill name")).toBeNull();
    expect(mocks.createBill).not.toHaveBeenCalled();
  });

  it("never sends from_subscription_id when editing an existing bill", async () => {
    mocks.listBills.mockResolvedValue([
      {
        id: 7,
        user_id: 1,
        name: "Rent",
        amount: "1500.00",
        category_id: null,
        frequency: "monthly",
        next_due_date: "2026-10-01",
        remind_days_before: 3,
        enabled: true,
        match_mode: "contains",
        match_term: "Rent",
        amount_tolerance: null,
        payment_count: 12,
        rolling_average: "1500.00",
      },
    ]);
    renderWithProviders(<Bills />, ["/bills"]);
    fireEvent.click(await screen.findByRole("button", { name: "Edit Rent" }));
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    await waitFor(() => expect(mocks.updateBill).toHaveBeenCalledTimes(1));
    expect(mocks.updateBill).toHaveBeenCalledWith(7, {
      name: "Rent",
      amount: 1500,
      // M123: the category scope is always stated on PATCH — `null` means
      // "no scope", which is what this bill had (and `exclude_unset` would
      // otherwise silently KEEP an old one while the select showed blank).
      category_id: null,
      frequency: "monthly",
      next_due_date: "2026-10-01",
    });
    const payload = mocks.updateBill.mock.calls[0][1] as Record<string, unknown>;
    expect(Object.keys(payload)).not.toContain("from_subscription_id");
  });
});

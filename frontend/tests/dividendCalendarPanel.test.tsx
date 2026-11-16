import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DividendCalendarPanel } from "../src/components/reports/DividendCalendarPanel";
import { setMasked } from "../src/lib/mask";
import { getDividendCalendar, type DividendCalendarResponse } from "../src/api/dividendCalendar";

vi.mock("../src/api/dividendCalendar", () => ({ getDividendCalendar: vi.fn() }));

const response: DividendCalendarResponse = {
  upcoming: [
    {
      security_id: 1,
      symbol: "BANK",
      frequency: "monthly",
      currency: "CAD",
      avg_amount: "118.40",
      annual_projection: "1420.80",
      last_payment_date: "2026-05-31",
      next_expected_date: "2026-06-30",
      num_payments: 2,
    },
    {
      security_id: 2,
      symbol: "FUND",
      frequency: "quarterly",
      currency: "USD",
      avg_amount: "85.00",
      annual_projection: "340.00",
      last_payment_date: "2026-05-31",
      next_expected_date: "2026-06-30",
      num_payments: 4,
    },
    {
      security_id: 3,
      symbol: "MIXED",
      frequency: "monthly",
      currency: null,
      avg_amount: null,
      annual_projection: null,
      last_payment_date: "2026-05-31",
      next_expected_date: "2026-06-30",
      num_payments: 2,
    },
  ],
  summary: {
    this_month: null,
    this_quarter: null,
    this_year: null,
    by_currency: [
      { currency: "CAD", this_month: "118.40", this_quarter: "118.40", this_year: "118.40" },
      { currency: "USD", this_month: "85.00", this_quarter: "85.00", this_year: "85.00" },
    ],
    unresolved_counts: { this_month: 1, this_quarter: 1, this_year: 1 },
  },
  // docs/07 §M184 — the projections are ledger-derived, and the strip says so.
  provenance: {
    status: "ok",
    as_of: "2026-05-31",
    sources: [
      {
        key: "income_trades",
        kind: "ledger",
        label: "recorded income trades",
        provider: null,
        as_of: "2026-05-31",
        age_days: null,
        status: "ok",
        detail: "Projected from 6 recorded income payment(s) since 2025-10-01 in your ledger — recorded trades, not a market-data feed. Projections are next-payment estimates, not forecasts.",
      },
    ],
  },
};

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DividendCalendarPanel />
    </QueryClientProvider>,
  );
}

describe("DividendCalendarPanel denomination and mask behavior", () => {
  beforeEach(() => {
    setMasked(false);
    vi.mocked(getDividendCalendar).mockResolvedValue(response);
  });

  it("states currency on averages, annual projections and each subtotal; masks instantly without refetch", async () => {
    mount();
    expect(await screen.findByText("Estimated income by currency — no FX conversion")).toBeTruthy();
    expect(screen.getAllByText("Excludes 1 projections with mixed or unknown currency")).toHaveLength(3);
    expect(screen.getAllByText("Mixed or unknown currency").length).toBeGreaterThan(1);
    expect(screen.getAllByText("2026-05-31")).toHaveLength(3);
    expect(screen.getAllByText("2026-06-30")).toHaveLength(3);
    expect(screen.getAllByText("CAD").length).toBeGreaterThan(1);
    expect(screen.getAllByText("USD").length).toBeGreaterThan(1);
    expect(getDividendCalendar).toHaveBeenCalledTimes(1);

    act(() => setMasked(true));
    await waitFor(() => expect(screen.getAllByText("•••••").length).toBeGreaterThan(0));
    expect(getDividendCalendar).toHaveBeenCalledTimes(1);

    act(() => setMasked(false));
    await waitFor(() => expect(screen.getAllByText("$118.40").length).toBeGreaterThan(0));
    expect(getDividendCalendar).toHaveBeenCalledTimes(1);
  });
});

describe("DividendCalendarPanel provenance — source, timestamp, stale-or-missing (docs/07 §M184)", () => {
  beforeEach(() => {
    setMasked(false);
    vi.mocked(getDividendCalendar).mockResolvedValue(response);
  });

  it("names the ledger source and the day of the newest payment — never as market data", async () => {
    mount();
    const strip = await screen.findByTestId("dividend-provenance");

    expect(strip.textContent).toContain("recorded income trades");
    expect(strip.textContent).toContain("2026-05-31");
    // A recorded fact answers ok/missing only — a projection is never stale.
    expect(strip.textContent).not.toContain("(stale)");
    expect(strip.textContent).not.toContain("(missing)");
    expect(strip.textContent).not.toContain("yfinance");
    expect(strip.getAttribute("title")).toContain("recorded trades, not a market-data feed");
  });

  it("the empty answer reports its source as missing instead of a silent blank", async () => {
    vi.mocked(getDividendCalendar).mockResolvedValue({
      ...response,
      upcoming: [],
      provenance: {
        status: "missing",
        as_of: null,
        sources: [
          {
            key: "income_trades",
            kind: "ledger",
            label: "recorded income trades",
            provider: null,
            as_of: null,
            age_days: null,
            status: "missing",
            detail: "No recorded income payments in the trailing 13 months — there is nothing to project.",
          },
        ],
      },
    });
    mount();

    expect(await screen.findByText("No dividend history")).toBeTruthy();
    const strip = screen.getByTestId("dividend-provenance");
    expect(strip.textContent).toContain("(missing)");
    expect(strip.textContent).not.toContain("2026-05-31");
    expect(strip.getAttribute("title")).toContain("nothing to project");
  });
});

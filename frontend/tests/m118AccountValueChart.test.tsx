// M118 — the account value chart (docs/08 §Account value history,
// docs/09 §Account value history).
//
// What is pinned here is the part that can look right and be wrong:
//   * the chart's ONE-sentence text alternative carries the facts a screen
//     reader gets instead of the SVG — endpoints, marker count, estimated
//     periods and the periods with NO valuation;
//   * an unvalued period says "no valuation for this period" and prints `—`
//     in the table — never a 0, never an interpolated number;
//   * the `estimated` badge is on the header chip AND the table row, so a
//     carried-forward price cannot pass as a same-day close;
//   * every marker has a real <button> in the data table (the keyboard path —
//     the pins inside role="img" are pointer-only by design) and firing it
//     hands the SAME event object to the page, which is what scrolls the
//     transaction/trade table to the row behind it;
//   * a brand-new account renders the three-part empty contract, not $0.
import { fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import {
  AccountValueChart,
  valueEventAmount,
  valueEventEvidence,
  valueEventHeading,
} from "../src/components/charts/AccountValueChart";
import type { AccountValueHistory, ValueHistoryEvent } from "../src/api/accounts";
import { ThemeProvider } from "../src/theme/ThemeProvider";

// jsdom has no matchMedia; ThemeProvider and useReducedMotion call it.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() {
      return false;
    },
  })) as unknown as typeof window.matchMedia;
}

const DEPOSIT: ValueHistoryEvent = {
  id: "transaction:456",
  source: "transaction",
  trade_id: null,
  transaction_id: 456,
  date: "2026-01-05",
  bucket_end: "2026-01-31",
  kind: "deposit",
  direction: 1,
  amount_cad: "16500.00",
  amount_native: "16500.00",
  currency: "CAD",
  payee: "Wealthsimple",
  category_name: "Deposit",
  symbol: null,
  security_id: null,
  quantity: null,
  action: null,
};

const WITHDRAWAL: ValueHistoryEvent = {
  ...DEPOSIT,
  id: "trade:9001",
  source: "trade",
  trade_id: 9001,
  transaction_id: null,
  date: "2026-02-10",
  kind: "withdrawal",
  direction: -1,
  amount_cad: "-24318.76",
  amount_native: "-24318.76",
  payee: null,
  category_name: null,
  action: "contribution",
  quantity: "0",
  security_id: 83,
  symbol: "CASH",
};

const IN_KIND: ValueHistoryEvent = {
  ...DEPOSIT,
  id: "trade:9002",
  source: "trade",
  trade_id: 9002,
  transaction_id: null,
  date: "2026-01-31",
  kind: "transfer_in",
  direction: 1,
  amount_cad: "13491.20",
  amount_native: "13491.20",
  payee: null,
  category_name: null,
  action: "transfer_in",
  quantity: "320",
  security_id: 84,
  symbol: "VCN",
};

function history(overrides: Partial<AccountValueHistory> = {}): AccountValueHistory {
  return {
    account_id: 731,
    from: "2026-01-01",
    to: "2026-03-31",
    granularity: "month",
    requested_granularity: "month",
    currency: "CAD",
    native_currency: "CAD",
    min_event_value: "1000.00",
    has_data: true,
    buckets: [
      {
        bucket_end: "2026-01-31",
        cash_cad: "16500.00",
        positions_cad: "18742.00",
        value_cad: "35242.00",
        estimated: false,
        valued: true,
      },
      {
        bucket_end: "2026-02-28",
        cash_cad: "16200.00",
        positions_cad: "18742.00",
        value_cad: "34942.00",
        estimated: true,
        valued: true,
      },
      {
        bucket_end: "2026-03-31",
        cash_cad: "16200.00",
        positions_cad: null,
        value_cad: null,
        estimated: false,
        valued: false,
      },
    ],
    events: [DEPOSIT, WITHDRAWAL],
    ...overrides,
  };
}

function renderChart(
  props: Partial<ComponentProps<typeof AccountValueChart>> = {},
  onSelectEvent = vi.fn(),
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <AccountValueChart accountId={731} history={history()} onSelectEvent={onSelectEvent} {...props} />
      </ThemeProvider>
    </QueryClientProvider>,
  );
  return { ...utils, onSelectEvent };
}

describe("M118 account value chart", () => {
  it("renders ONE role=img with the series facts, and a focusable scroll region", () => {
    renderChart();
    const frame = screen.getByRole("img");
    const label = frame.getAttribute("aria-label") ?? "";
    expect(label).toContain("Account value over time — monthly buckets");
    expect(label).toContain("3 points");
    expect(label).toContain("from $35,242.00 to $34,942.00");
    expect(label).toContain("2 markers for contributions, withdrawals and transfers");
    expect(label).toContain("1 estimated period");
    expect(label).toContain("1 period without a valuation");

    const region = screen.getByRole("region", { name: "Account value over time chart" });
    expect(region.tabIndex).toBe(0);
    expect(region.className).toContain("overflow-x-auto");
  });

  it("flags the estimated period and the gap in the header chips", () => {
    renderChart();
    expect(screen.getByText("~ 1 estimated")).toBeTruthy();
    expect(screen.getByText("1 without a valuation")).toBeTruthy();
  });

  it("prints 'no valuation for this period' instead of a zero", () => {
    renderChart();
    fireEvent.click(screen.getByText("Show the numbers behind this chart"));
    // The unvalued March bucket: value cell is an em dash, note is the copy.
    expect(screen.getAllByText("no valuation for this period").length).toBeGreaterThan(0);
    expect(screen.getAllByText("estimated").length).toBeGreaterThan(0);
    // Nothing anywhere prints a fabricated 0 for that bucket.
    const rows = screen.getAllByRole("row");
    const march = rows.find((row) => row.textContent?.includes("Mar 2026"));
    expect(march?.textContent ?? "").toContain("—");
    expect(march?.textContent ?? "").not.toContain("$0.00");
  });

  it("lists every marker with its evidence and hands the event back on Show", () => {
    const { onSelectEvent } = renderChart();
    fireEvent.click(screen.getByText("Show the numbers behind this chart"));
    expect(screen.getByText("deposit")).toBeTruthy();
    expect(screen.getByText("withdrawal")).toBeTruthy();
    expect(screen.getByText("+ $16,500.00")).toBeTruthy();
    expect(screen.getByText("− $24,318.76")).toBeTruthy();
    expect(screen.getByText("categorized Deposit — from Wealthsimple")).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "Show the deposit of 2026-01-05 in the table" }),
    );
    expect(onSelectEvent).toHaveBeenCalledTimes(1);
    expect(onSelectEvent.mock.calls[0]?.[0]).toEqual(DEPOSIT);
  });

  it("renders the three-part empty state when the account has no history", () => {
    renderChart({ history: history({ has_data: false }) });
    expect(screen.getByTestId("empty-state-title").textContent).toBe("No value history yet");
    // No chart frame (the preview <img> is a different role=img).
    expect(screen.queryByRole("img", { name: /Account value over time/ })).toBeNull();
    expect(screen.getByRole("link", { name: /Import a statement/ })).toBeTruthy();
  });

  it("says which rows a marker came from — trade evidence is never invented", () => {
    expect(valueEventHeading(WITHDRAWAL)).toBe("− $24,318.76 withdrawal");
    expect(valueEventAmount(WITHDRAWAL)).toBe("− $24,318.76");
    expect(valueEventEvidence(WITHDRAWAL)).toBe("recorded on the trades ledger");
    expect(valueEventEvidence(IN_KIND)).toBe("320 VCN transferred in kind");
    expect(valueEventEvidence(DEPOSIT)).toBe("categorized Deposit — from Wealthsimple");
    // A transaction with neither category nor payee drops the clause instead
    // of printing a placeholder.
    expect(
      valueEventEvidence({ ...DEPOSIT, category_name: null, payee: null }),
    ).toBeNull();
  });
});

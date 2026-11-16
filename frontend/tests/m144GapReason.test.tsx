// M144 — the two-reason empty state (docs/08 §Honesty, docs/09 §Empty states).
//
// A period with no value can mean two different things, and printing one
// sentence for both is how a reader ends up believing their price cache is
// broken on days when nothing was held yet:
//   * `no_price` / `fx` — a DATA gap: red "no valuation for this period", and
//     it is the only thing the header's red "N without a valuation" chip counts;
//   * `not_held` — a HOLDINGS gap: muted "not held yet", never the red badge.
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import {
  AccountValueChart,
  gapReasonNote,
} from "../src/components/charts/AccountValueChart";
import type { AccountValueHistory, ValueHistoryGapReason } from "../src/api/accounts";
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

function bucket(
  bucket_end: string,
  valued: boolean,
  gap_reason: ValueHistoryGapReason | null,
  value_cad: string | null = null,
) {
  return {
    bucket_end,
    cash_cad: value_cad,
    positions_cad: value_cad,
    value_cad,
    estimated: false,
    valued,
    gap_reason,
  };
}

function history(gapReason: ValueHistoryGapReason | null): AccountValueHistory {
  return {
    account_id: 459,
    from: "2026-01-01",
    to: "2026-01-03",
    granularity: "day",
    requested_granularity: "day",
    currency: "CAD",
    native_currency: "CAD",
    min_event_value: "1000.00",
    has_data: true,
    buckets: [
      bucket("2026-01-01", false, gapReason),
      bucket("2026-01-02", false, gapReason),
      bucket("2026-01-03", true, null, "51000.00"),
    ],
    events: [],
  };
}

function renderChart(gapReason: ValueHistoryGapReason | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <AccountValueChart
          accountId={459}
          title="Princess TFSA"
          history={history(gapReason)}
          onSelectEvent={vi.fn()}
        />
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

describe("M144 two-reason empty state", () => {
  it("maps each reason to its own words and tone", () => {
    expect(gapReasonNote("not_held")).toEqual({ text: "not held yet", tone: "muted" });
    expect(gapReasonNote("no_price")).toEqual({
      text: "no valuation for this period",
      tone: "danger",
    });
    // An unreasoned gap (an older payload) keeps the data-gap sentence.
    expect(gapReasonNote(null).text).toBe("no valuation for this period");
  });

  it("says 'not held yet' for a holdings gap and never wears the red badge", () => {
    renderChart("not_held");
    fireEvent.click(screen.getByText("Show the numbers behind this chart"));
    expect(screen.getAllByText("not held yet").length).toBeGreaterThan(0);
    expect(screen.queryByText("no valuation for this period")).toBeNull();
    // The red chip counts DATA gaps only — a day nothing was held is not one.
    expect(screen.queryByText("2 without a valuation")).toBeNull();
    const label = screen.getByRole("img").getAttribute("aria-label") ?? "";
    expect(label).not.toContain("without a valuation");
  });

  it("keeps the data-gap sentence and the red badge for a real gap", () => {
    renderChart("no_price");
    fireEvent.click(screen.getByText("Show the numbers behind this chart"));
    expect(screen.getAllByText("no valuation for this period").length).toBeGreaterThan(0);
    expect(screen.queryByText("not held yet")).toBeNull();
    expect(screen.getByText("2 without a valuation")).toBeTruthy();
    const label = screen.getByRole("img").getAttribute("aria-label") ?? "";
    expect(label).toContain("2 periods without a valuation");
  });
});

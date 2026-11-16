// The Taxes page's tax-loss alerts render money through the ONE CAD helper
// (`lib/format::formatCad`), not a page-local shim (docs/46 §D6, docs/09
// §Display masking).
//
// The local shim this file replaced held its own rounding AND its own fallback:
// `parseFloat("abc")` → NaN → a fabricated "$0". The shared helper renders an
// unparseable/blank/null Decimal-string as the honest "—" (never a figure the
// wire did not send), which is the documented contract for every money
// surface. Three things are pinned here:
//
//   1. valid backend Decimal-strings render identically to the shared helper
//      (values, cents, thousands separators, the signed loss column);
//   2. a malformed value renders "—" — NEVER "$0";
//   3. the panel keeps its `useMasked()` subscription: one flip re-masks every
//      figure in place and un-masking restores them.
//
// House matcher style: plain DOM reads, no jest-dom.
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getTaxLossAlerts } from "../src/api/taxAlerts";
import type { TaxAlertsResponse } from "../src/api/taxAlerts";
import { TaxAlertsPanel } from "../src/components/reports/TaxAlertsPanel";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";
import { formatCad } from "../src/lib/format";

vi.mock("../src/api/taxAlerts", () => ({
  getTaxLossAlerts: vi.fn(),
}));

function payload(over: Partial<TaxAlertsResponse> = {}): TaxAlertsResponse {
  return {
    alerts: [
      {
        security_id: 7,
        symbol: "AAA",
        account_id: 1,
        account_name: "TFSA",
        quantity: "100",
        book_cost_cad: "12500.00",
        market_value_cad: "11999.90",
        unrealized_loss_cad: "500.10",
        estimated_tax_benefit_cad: "75.02",
        superficial_loss_risk: false,
      },
    ],
    summary: {
      count: 1,
      total_unrealized_loss_cad: "500.10",
      total_estimated_tax_benefit_cad: "75.02",
    },
    ...over,
  };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TaxAlertsPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
});

afterEach(() => {
  sessionStorage.clear();
  initMaskForSession();
  vi.clearAllMocks();
});

describe("TaxAlertsPanel — money via the shared CAD helper", () => {
  it("renders valid backend Decimal-strings exactly like `lib/format::formatCad`", async () => {
    vi.mocked(getTaxLossAlerts).mockResolvedValue(payload());
    const { container } = renderPanel();

    await screen.findByText("AAA");
    const text = container.textContent ?? "";

    // The summary card and the row's four money cells.
    expect(text).toContain(`Total loss: ${formatCad("500.10")}`);
    expect(text).toContain(`Est. tax benefit: ${formatCad("75.02")}`);
    expect(text).toContain(formatCad("12500.00"));
    expect(text).toContain(formatCad("11999.90"));
    expect(text).toContain(formatCad("75.02"));
    // The loss column is rendered as a signed magnitude ("-", then the figure).
    expect(text).toContain(`-${formatCad("500.10")}`);

    // …which pins these exact strings, so a formatter swap cannot silently
    // change the display (thousands separators, forced cents, "$" prefix).
    expect(text).toContain("$12,500.00");
    expect(text).toContain("$11,999.90");
    expect(text).toContain("-$500.10");
    expect(text).toContain("$75.02");
  });

  it("renders a malformed Decimal-string as the honest dash — never a fabricated $0", async () => {
    vi.mocked(getTaxLossAlerts).mockResolvedValue(
      payload({
        alerts: [
          {
            security_id: 8,
            symbol: "BBB",
            account_id: 1,
            account_name: "TFSA",
            quantity: "10",
            book_cost_cad: "not-a-number",
            market_value_cad: "",
            unrealized_loss_cad: "NaN",
            estimated_tax_benefit_cad: "250.00",
            superficial_loss_risk: true,
          },
        ],
      }),
    );
    const { container } = renderPanel();

    await screen.findByText("BBB");
    const text = container.textContent ?? "";
    expect(text).not.toContain("$0");
    // Four money cells, three of them malformed → three honest dashes; the
    // one valid figure still renders normally.
    expect(text.match(/—/g)?.length).toBe(3);
    expect(text).toContain(formatCad("250.00"));
  });

  it("keeps the useMasked() subscription: one flip re-masks every figure", async () => {
    vi.mocked(getTaxLossAlerts).mockResolvedValue(payload());
    const { container } = renderPanel();

    await screen.findByText("AAA");
    expect(container.textContent).toContain("$12,500.00");

    act(() => setMasked(true));
    await waitFor(() => expect(container.textContent).not.toContain("$12,500.00"));
    expect(container.textContent).not.toContain("$500.10");
    expect(container.textContent).not.toContain("$75.02");
    expect((container.textContent ?? "").match(/•••••/g)?.length).toBe(6);

    act(() => setMasked(false));
    await waitFor(() => expect(container.textContent).toContain("$12,500.00"));
    expect(container.textContent).toContain("-$500.10");
    expect(container.textContent).not.toContain(MASK_TEXT);
  });
});

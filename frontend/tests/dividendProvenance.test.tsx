// docs/07 §M184 — every dividend view states WHICH source produced its
// numbers, WHEN that source last answered, and whether it is stale or missing.
//
// Three seams, one vocabulary:
//   * `GET /reports/dividends`       → ledger income trades + trade-date FX cache
//   * `GET /reports/dividend-calendar` → ledger income trades (never market data)
//   * `GET /trades/dividend-suggestions` → the flat X-Dividend-History-State
//     header, folded into the same shape by `provenanceFromHistory`
//
// These are behaviour assertions: the panels are RENDERED with fixture payloads
// and the strip's own text/tooltip/class is read back out of the DOM.

import React from "react";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../src/theme/ThemeProvider";
import { DividendsPanel } from "../src/components/reports/DividendsPanel";
import { DividendProvenanceStrip } from "../src/components/reports/DividendProvenanceStrip";
import { provenanceFromHistory } from "../src/api/dividendProvenance";
import { getDividendReport, type DividendReport } from "../src/api/reports";
import { setMasked } from "../src/lib/mask";

// jsdom has no matchMedia; ThemeProvider and useReducedMotion both call it.
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

vi.mock("../src/api/reports", () => ({ getDividendReport: vi.fn() }));

// Recharts' ResponsiveContainer measures the DOM, which jsdom cannot do; give
// the one chart the panel draws a fixed box (reportMoneyAxes' shim).
vi.mock("recharts", async () => {
  const mod = await import("recharts");
  const ReactMod = await import("react");
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => {
      const child = Array.isArray(children) ? children[0] : children;
      if (child && typeof child === "object" && "type" in (child as object)) {
        return ReactMod.cloneElement(child as React.ReactElement, { width: 480, height: 320 });
      }
      return child;
    },
  };
});

/** A USD report whose EUR payment has no cached rate — one ok source, one missing. */
const REPORT: DividendReport = {
  year: 2026,
  available_years: [2026, 2025],
  items: [
    {
      security_id: 7,
      symbol: "VTI",
      name: "Vanguard Total Stock Market",
      dividend_cad: "100.00",
      distribution_cad: "0.00",
      roc_cad: "0.00",
      total_cad: "100.00",
      payment_count: 2,
      last_payment_date: "2026-02-10",
    },
  ],
  by_month: [
    { month: "2026-01", total_cad: "0.00" },
    { month: "2026-02", total_cad: "100.00" },
  ],
  totals: { dividend_cad: "100.00", distribution_cad: "0.00", roc_cad: "0.00", total_cad: "100.00" },
  ttm_total_cad: "100.00",
  unresolved_fx: 1,
  provenance: {
    // Worst source status; the OLDEST timestamp across sources.
    status: "missing",
    as_of: "2026-02-10",
    sources: [
      {
        key: "income_trades",
        kind: "ledger",
        label: "recorded income trades",
        provider: null,
        as_of: "2026-02-10",
        age_days: null,
        status: "ok",
        detail:
          "Your committed income trades recorded in 2026 — newest payment 2026-02-10. Ledger facts, not a market-data feed.",
      },
      {
        key: "trade_date_fx",
        kind: "market_data",
        label: "trade-date FX cache",
        provider: "boc_valet",
        as_of: "2026-09-01",
        age_days: 44,
        status: "missing",
        detail:
          "1 payment(s) in 2026 have no cached rate for EUR→CAD and are excluded from every CAD total. Reports are cache-only: no rate was fetched.",
      },
    ],
  },
};

const EMPTY_REPORT: DividendReport = {
  year: 2026,
  available_years: [],
  items: [],
  by_month: [],
  totals: { dividend_cad: "0.00", distribution_cad: "0.00", roc_cad: "0.00", total_cad: "0.00" },
  ttm_total_cad: "0.00",
  unresolved_fx: 0,
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
        detail: "No income trades recorded in 2026: nothing to report from your ledger.",
      },
    ],
  },
};

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <ThemeProvider>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </ThemeProvider>
  );
}

function mountPanel() {
  return render(<DividendsPanel />, { wrapper });
}

beforeEach(() => {
  setMasked(false);
  vi.mocked(getDividendReport).mockResolvedValue(REPORT);
});

describe("DividendsPanel provenance (docs/07 §M184, docs/12 §Dividends)", () => {
  it("names BOTH sources with their own status — ledger ok, FX missing — and explains the exclusion", async () => {
    mountPanel();
    const strip = await screen.findByTestId("dividend-provenance");

    expect(strip.textContent).toContain("recorded income trades");
    expect(strip.textContent).toContain("2026-02-10");
    expect(strip.textContent).toContain("trade-date FX cache");
    // The market-data source names WHO filled the book and how old it is.
    expect(strip.textContent).toContain("boc_valet");
    expect(strip.textContent).toContain("44d");
    // Exactly one of the two sources is missing, so the strip says so once.
    expect(strip.textContent.match(/\(missing\)/g)).toHaveLength(1);
    expect(strip.textContent).not.toContain("(stale)");
    expect(strip.getAttribute("title")).toContain("no cached rate for EUR→CAD");
    expect(strip.getAttribute("title")).toContain("not a market-data feed");
  });

  it("reports the missing ledger source above the empty state instead of a silent blank", async () => {
    vi.mocked(getDividendReport).mockResolvedValue(EMPTY_REPORT);
    mountPanel();

    expect(await screen.findByText("No dividend income recorded for 2026")).toBeTruthy();
    const strip = screen.getByTestId("dividend-provenance");
    expect(strip.textContent).toContain("recorded income trades");
    expect(strip.textContent).toContain("(missing)");
    expect(strip.textContent).not.toContain("trade-date FX cache");
    expect(strip.getAttribute("title")).toContain("nothing to report from your ledger");
  });
});

describe("provenanceFromHistory — the dividend-suggestions header in the same words", () => {
  const clean = {
    pending: 0,
    unavailable: 0,
    empty: 0,
    stale: 0,
    missing: 0,
    as_of: "2026-10-14T04:00:00Z",
    source: "yfinance",
    age_days: 0,
  };
  const fresh = { ...clean, empty: 1 };

  it("a never-successful source is MISSING, never stale and never 'no dividends'", () => {
    const block = provenanceFromHistory({
      ...fresh,
      pending: 2,
      empty: 0,
      missing: 2,
      as_of: null,
      age_days: null,
    });
    expect(block.status).toBe("missing");
    expect(block.as_of).toBeNull();
    expect(block.sources[0]).toMatchObject({
      key: "dividend_history",
      kind: "market_data",
      provider: "yfinance",
      status: "missing",
      as_of: null,
    });
    expect(block.sources[0].detail).toContain("no provider history yet");
    expect(block.sources[0].detail).toContain("in flight");
  });

  it("a stale count downgrades a source that HAS answered, keeping its timestamp", () => {
    const block = provenanceFromHistory({ ...fresh, empty: 0, stale: 3 });
    expect(block.status).toBe("stale");
    expect(block.sources[0].status).toBe("stale");
    // The header timestamp is UTC; the strip shows the day it answered.
    expect(block.sources[0].as_of).toBe("2026-10-14");
    expect(block.sources[0].age_days).toBe(0);
    expect(block.sources[0].detail).toContain("over 24h ago");
  });

  it("a clean, recent source answers ok and says when it refreshed", () => {
    const block = provenanceFromHistory(clean);
    expect(block.status).toBe("ok");
    expect(block.sources[0].status).toBe("ok");
    expect(block.sources[0].detail).toContain("last refreshed 2026-10-14T04:00:00Z");
  });
});

describe("DividendProvenanceStrip", () => {
  it("marks a stale market-data source with a pulsing danger badge", () => {
    render(
      <DividendProvenanceStrip
        provenance={{
          status: "stale",
          as_of: "2026-10-01",
          sources: [
            {
              key: "dividend_history",
              kind: "market_data",
              label: "yfinance dividend history",
              provider: "yfinance",
              as_of: "2026-10-01",
              age_days: 14,
              status: "stale",
              detail: "1 source last refreshed over 24h ago.",
            },
          ],
        }}
      />,
    );

    const badge = screen.getByText(/yfinance dividend history/);
    expect(badge.textContent).toContain("(stale)");
    expect(badge.textContent).toContain("14d");
    expect(badge.className).toContain("border-danger/40");
    // The pulse lives on the badge's leading dot (Badge's signature treatment).
    expect(badge.querySelector("span")?.className).toContain("animate-pulse");
  });

  it("renders nothing when there is no payload to report", () => {
    const { container } = render(<DividendProvenanceStrip provenance={null} />);
    expect(container.firstChild).toBeNull();
  });
});

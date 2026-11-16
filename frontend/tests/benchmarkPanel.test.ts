import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getBenchmarkComparison } from "../src/api/benchmarks";
import { BenchmarkPanel } from "../src/components/investments/BenchmarkPanel";

vi.mock("../src/api/benchmarks", () => ({
  getBenchmarkComparison: vi.fn(),
}));

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return render(
    createElement(BenchmarkPanel, {
      startDate: "2025-01-05",
      endDate: "2025-02-05",
    }),
    { wrapper },
  );
}

/** Two benchmark series points (>= 2) = real data; EXCESS renders normally.
 *  SPY is the USD listing, so the return shown is the FX-inclusive CAD one. */
const comparison = {
  portfolio_start: "100.00",
  portfolio_end: "110.00",
  portfolio_return_pct: "10.00",
  benchmark_symbol: "SPY",
  benchmark_name: "S&P 500",
  benchmark_return_pct: "5.00",
  benchmark_points: 2,
  benchmark_currency: "CAD",
  benchmark_listing_currency: "USD",
  benchmark_series_start: "2024-01-02",
  benchmark_series_end: "2025-02-05",
  benchmark_clipped: false,
  excess_return_pct: "5.00",
  start_date: "2025-01-05",
  end_date: "2025-02-05",
};

describe("BenchmarkPanel", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("formats decimal wire values returned as strings or numbers", async () => {
    vi.mocked(getBenchmarkComparison).mockResolvedValue(comparison);

    renderPanel();

    await waitFor(() => {
      expect(screen.getAllByText("+10.00%")).toHaveLength(1);
      expect(screen.getAllByText("+5.00%")).toHaveLength(2);
    });
  });

  it("labels an FX-inclusive benchmark return with both currencies (M173)", async () => {
    vi.mocked(getBenchmarkComparison).mockResolvedValue(comparison);

    renderPanel();

    expect(
      await screen.findByText(/S&P 500 is quoted in USD; the return above is FX-inclusive, converted to CAD\./),
    ).toBeTruthy();
  });

  it("names the coverage when the window is clipped (M173)", async () => {
    vi.mocked(getBenchmarkComparison).mockResolvedValue({
      ...comparison,
      benchmark_clipped: true,
      benchmark_series_start: "2025-01-20",
    });

    renderPanel();

    expect(await screen.findByText(/Benchmark data starts .* this window is clipped\./)).toBeTruthy();
  });

  it("prints the window in the ISO dates it was computed for (M173)", async () => {
    // `new Date("2025-01-05")` is UTC midnight, so rendering it in the
    // browser's zone printed the day BEFORE the window west of Greenwich.
    vi.mocked(getBenchmarkComparison).mockResolvedValue(comparison);

    renderPanel();

    expect(
      await screen.findByText(/Jan\.? 5, 2025 — Feb\.? 5, 2025/),
    ).toBeTruthy();
  });

  it("gates EXCESS on real benchmark data — <2 points is no data, not alpha (M65 / m61 #7)", async () => {
    // excess == portfolio return: without the gate this reads as +10% alpha
    // manufactured out of the documented numeric-0 placeholder.
    vi.mocked(getBenchmarkComparison).mockResolvedValue({
      ...comparison,
      benchmark_points: 0,
      benchmark_return_pct: null,
      excess_return_pct: null,
    });

    renderPanel();

    expect(await screen.findByText("No benchmark data for this window")).toBeTruthy();
    // Portfolio still shows; EXCESS must NOT echo it as alpha.
    expect(screen.getAllByText("+10.00%")).toHaveLength(1);
    // The selected chip is visibly disabled; the other chips stay clickable
    // (each fetches its own comparison).
    const selectedChip = screen.getByRole("button", { name: "SPY" }) as HTMLButtonElement;
    expect(selectedChip.disabled).toBe(true);
    expect(selectedChip.getAttribute("title")).toBe("No benchmark data for this window");
    expect((screen.getByRole("button", { name: "XIU" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("treats a null benchmark return as no data even with enough points (M173)", async () => {
    // The series HAS closes but cannot be expressed in the reporting currency
    // (no cached USD/CAD): the panel must say so, never print a fabricated
    // "+0.00%" beside a real portfolio return.
    vi.mocked(getBenchmarkComparison).mockResolvedValue({
      ...comparison,
      benchmark_points: 5,
      benchmark_return_pct: null,
      excess_return_pct: null,
    });

    renderPanel();

    expect(await screen.findByText("No benchmark data for this window")).toBeTruthy();
    expect(screen.queryByText("+0.00%")).toBeNull();
    expect(screen.getAllByText("+10.00%")).toHaveLength(1);
  });
});

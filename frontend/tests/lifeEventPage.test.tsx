// docs/58 named tests E1–E6 — the /retirement life-event cards over the SAME
// two mocked reads R7–R11/R13 use (net-worth + cashflow). No new data source:
// every fetch is still one of the two report endpoints, and every year is
// re-derived from the clock — never a date literal.

import { act, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Retirement } from "../src/pages/Retirement";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";
import { PLAN_END_AGE, SIM_DEBOUNCE_MS } from "../src/lib/retirementEngine";

// jsdom has no matchMedia (ThemeProvider / reduced-motion read it).
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: /min-width:\s*(\d+)px/.test(query) ? window.innerWidth >= 768 : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

// ResponsiveContainer measures the DOM, which jsdom cannot do — hand the
// chart a fixed box so the axes really draw (house shim, m109/f7).
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

/** Mutable server answers — each test may re-point them before rendering. */
const h = vi.hoisted(() => ({
  calls: [] as string[],
  netWorth: {
    buckets: [
      { bucket_end: "2026-11-10", cash_cad: "20000.00", positions_cad: "30000.00", net_cad: "50000.00" },
    ],
  },
  cashflow: {
    buckets: [
      { bucket: "2025-12-31", income_cad: "7000.00", expenses_cad: "4000.00", net_cad: "3000.00" },
      { bucket: "2026-01-31", income_cad: "7000.00", expenses_cad: "6000.00", net_cad: "1000.00" },
      { bucket: "2026-02-28", income_cad: "7000.00", expenses_cad: "8000.00", net_cad: "-1000.00" },
    ],
  },
}));

const DEFAULT_NET_WORTH = JSON.stringify(h.netWorth);
const DEFAULT_CASHFLOW = JSON.stringify(h.cashflow);

/** Minimal Response shape covering exactly what src/api/client.ts reads. */
function fakeResponse(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText: status === 204 ? "No Content" : "OK",
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

function handle(input: RequestInfo | URL): Response {
  const url = String(input);
  h.calls.push(url);
  const path = url.split("?")[0];
  if (path === "/api/v1/reports/net-worth") return fakeResponse(200, h.netWorth);
  if (path === "/api/v1/reports/cashflow") return fakeResponse(200, h.cashflow);
  return fakeResponse(404, { detail: `unexpected fetch: ${url}`, code: "not_found" });
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    <MemoryRouter initialEntries={["/retirement"]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <Retirement />
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** The current calendar year — every expectation below re-derives from it. */
function baseYear(): number {
  return new Date().getFullYear();
}

function timelineRows(): HTMLTableRowElement[] {
  return Array.from(
    screen.getByTestId("life-event-timeline").querySelectorAll<HTMLTableRowElement>("tbody tr"),
  );
}

function rowForYear(year: number): HTMLTableRowElement {
  const row = timelineRows().find(
    (tr) => tr.querySelectorAll("td")[0].textContent === String(year),
  );
  expect(row, `timeline row for ${year}`).toBeDefined();
  return row as HTMLTableRowElement;
}

function impactOf(row: HTMLTableRowElement): string | null {
  return row.querySelectorAll("td")[4].textContent;
}

function parseDelta(text: string | null): { withPct: number; withoutPct: number } {
  const withMatch = /Success with events: ([\d.]+)%/.exec(text ?? "");
  const withoutMatch = /without events: ([\d.]+)%/.exec(text ?? "");
  expect(withMatch, `delta line has a with-%: ${text}`).not.toBeNull();
  expect(withoutMatch, `delta line has a without-%: ${text}`).not.toBeNull();
  return {
    withPct: Number(withMatch?.[1]),
    withoutPct: Number(withoutMatch?.[1]),
  };
}

async function addDefaultHomePurchase(): Promise<void> {
  fireEvent.click(screen.getByTestId("life-event-submit"));
  await screen.findByTestId("life-event-item");
}

beforeEach(() => {
  h.calls.length = 0;
  h.netWorth = JSON.parse(DEFAULT_NET_WORTH) as typeof h.netWorth;
  h.cashflow = JSON.parse(DEFAULT_CASHFLOW) as typeof h.cashflow;
  window.localStorage.clear();
  window.sessionStorage.clear();
  initMaskForSession(); // boot contract: unmasked, key cleared
  vi.stubGlobal("fetch", vi.fn(handle));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("E1 — no events: the baseline timeline, nothing invented", () => {
  it("years rows clock-derived, every impact $0.00, no delta, no items, only the two endpoints", async () => {
    const { container } = renderPage();
    await screen.findByTestId("retirement-success");

    const age = Number(
      (screen.getByLabelText("Current age (18–90)") as HTMLInputElement).value,
    );
    const years = PLAN_END_AGE - age;
    const rows = timelineRows();
    expect(rows).toHaveLength(years);
    expect(rows[0].querySelectorAll("td")[0].textContent).toBe(String(baseYear() + 1));
    expect(rows[rows.length - 1].querySelectorAll("td")[0].textContent).toBe(
      String(baseYear() + years),
    );
    for (const row of rows) {
      expect(impactOf(row)).toBe("$0.00");
      expect(row.querySelectorAll("td")[3].textContent).toBe("—"); // nothing starts here
    }

    expect(screen.queryByTestId("life-event-success-delta")).toBeNull();
    expect(screen.queryAllByTestId("life-event-item")).toHaveLength(0);
    expect(screen.getByText(/No events yet/)).toBeTruthy();

    // Still exactly the two report reads — no new data source.
    expect(h.calls.length).toBeGreaterThan(0);
    expect(h.calls.every((u) => /\/api\/v1\/reports\/(net-worth|cashflow)/.test(u))).toBe(true);
    expect(container.querySelector('[data-testid="life-event-error"]')).toBeNull();
  });
});

describe("E2 — add: one click commits, plan + delta + timeline move together", () => {
  it("appends the item, flips the verdict to the with-events run, writes the impact row", async () => {
    renderPage();
    const before = (await screen.findByTestId("retirement-success")).textContent;
    expect(before).toBe("8.0%"); // the fixture R9 pins, as the baseline

    await addDefaultHomePurchase();

    const delta = await screen.findByTestId("life-event-success-delta");
    const parsed = parseDelta(delta.textContent);
    expect(parsed.withoutPct).toBe(8.0); // baseline run, same seed
    expect(parsed.withPct).toBeLessThan(parsed.withoutPct);
    // The headline IS the with-events verdict — same number the delta line leads with.
    expect(screen.getByTestId("retirement-success").textContent).toBe(
      `${parsed.withPct.toFixed(1)}%`,
    );
    expect(screen.getByTestId("retirement-success").textContent).not.toBe(before);

    // The default home purchase (100,000 down, blank year → first plan year)
    // lands on that year's impact cell; the label names the event.
    const row = rowForYear(baseYear() + 1);
    expect(impactOf(row)).toBe("-$100,000.00");
    expect(row.querySelectorAll("td")[3].textContent).toContain("Home purchase");

    // No endpoint was added by any of it.
    expect(h.calls.every((u) => /\/api\/v1\/reports\/(net-worth|cashflow)/.test(u))).toBe(true);
  });
});

describe("E3 — edit + remove: both directions update everything", () => {
  it("editing the down payment moves the summary, the impact row and the delta; remove restores", async () => {
    renderPage();
    const before = (await screen.findByTestId("retirement-success")).textContent;
    await addDefaultHomePurchase();

    const year = baseYear() + 1;
    const firstWith = parseDelta(screen.getByTestId("life-event-success-delta").textContent)
      .withPct;

    // Edit the DOWN PAYMENT 100,000 → 1,000: the item summary and the
    // impact row follow, and a smaller hole survives strictly more paths.
    fireEvent.click(screen.getByRole("button", { name: `Edit Home purchase ${year}` }));
    const downPayment = screen.getByTestId("life-event-field-downPayment") as HTMLInputElement;
    expect(downPayment.value).toBe("100000.00"); // prefilled from the stored event
    fireEvent.change(downPayment, { target: { value: "1000" } });
    fireEvent.click(screen.getByTestId("life-event-submit")); // "Save event"

    const item = await screen.findByTestId("life-event-item");
    expect(item.textContent).toContain("$1,000.00");
    expect(impactOf(rowForYear(year))).toBe("-$1,000.00");

    const parsed = parseDelta(
      (await screen.findByTestId("life-event-success-delta")).textContent,
    );
    expect(parsed.withPct).toBeGreaterThan(firstWith);
    expect(parsed.withoutPct).toBe(8.0); // the baseline never moved

    // Remove → baseline restored end to end.
    fireEvent.click(screen.getByRole("button", { name: `Remove Home purchase ${year}` }));
    expect(screen.queryAllByTestId("life-event-item")).toHaveLength(0);
    expect(screen.queryByTestId("life-event-success-delta")).toBeNull();
    expect(screen.getByTestId("retirement-success").textContent).toBe(before);
    for (const row of timelineRows()) {
      expect(impactOf(row)).toBe("$0.00");
    }
  });
});

describe("E4 — invalid input: the message shows, nothing is added", () => {
  it("an out-of-range year surfaces life-event-error and leaves the verdict on the baseline", async () => {
    renderPage();
    await screen.findByTestId("retirement-success");

    fireEvent.change(screen.getByTestId("life-event-year"), { target: { value: "1999" } });
    fireEvent.click(screen.getByTestId("life-event-submit"));

    const error = await screen.findByTestId("life-event-error");
    expect(error.getAttribute("role")).toBe("alert");
    expect(error.textContent).toMatch(
      new RegExp(`year must be an integer in ${baseYear() + 1}\\.\\.\\d{4} \\(got 1999\\)`),
    );
    expect(screen.queryAllByTestId("life-event-item")).toHaveLength(0);
    expect(screen.queryByTestId("life-event-success-delta")).toBeNull();
    expect(screen.getByTestId("retirement-success").textContent).toBe("8.0%");
  });
});

describe("E5 — display masking over the event money (docs/46 §D2)", () => {
  it("timeline money cells go to bullets, the delta percentages stay readable, inputs stay raw", async () => {
    const { container } = renderPage();
    await screen.findByTestId("retirement-success");
    await addDefaultHomePurchase();
    await screen.findByTestId("life-event-success-delta");

    const moneyCells = () =>
      Array.from(container.querySelectorAll("tbody td.tabular")).map((td) => td.textContent ?? "");
    expect(moneyCells().length).toBeGreaterThan(0);

    act(() => setMasked(true));
    expect(moneyCells().every((text) => text === MASK_TEXT)).toBe(true);
    // Percentages in the delta line are readable (D2's percentage ruling).
    expect(screen.getByTestId("life-event-success-delta").textContent ?? "").toMatch(/\d/);
    expect(screen.getByTestId("retirement-success").textContent).toMatch(/\d/);
    // Form inputs are controls, not figures — they keep their raw value.
    expect((screen.getByTestId("life-event-field-downPayment") as HTMLInputElement).value).toBe(
      "100000.00",
    );

    act(() => setMasked(false));
    expect(moneyCells().some((text) => text.includes("$"))).toBe(true);
  });
});

describe("E6 — stale horizon: edited assumptions invalidate the set without crashing", () => {
  it("raising the current age past the event's year shows the error and falls back to the baseline", async () => {
    renderPage();
    const before = (await screen.findByTestId("retirement-success")).textContent;
    expect(before).toBe("8.0%");

    // An event 30 years out — in range while the horizon is 55 years.
    fireEvent.change(screen.getByTestId("life-event-year"), {
      target: { value: String(baseYear() + 30) },
    });
    await addDefaultHomePurchase();
    expect(screen.getByTestId("life-event-success-delta")).toBeTruthy();

    // Raise the current age to 70 → horizon 25 years → the event is now
    // outside the plan. Debounced like every other assumption (docs/57 §4).
    vi.useFakeTimers();
    fireEvent.change(screen.getByLabelText("Current age (18–90)"), { target: { value: "70" } });
    act(() => {
      vi.advanceTimersByTime(SIM_DEBOUNCE_MS);
    });

    // findBy* would deadlock on fake timers (its polling interval is faked
    // too) — act() already flushed the render, so read it synchronously.
    const error = screen.getByTestId("life-event-error");
    expect(error.textContent).toMatch(/year must be an integer in/);
    // The event is never silently dropped…
    expect(screen.queryAllByTestId("life-event-item")).toHaveLength(1);
    // …the plan falls back to the event-free baseline and keeps rendering…
    expect(screen.queryByTestId("life-event-success-delta")).toBeNull();
    expect(screen.getByTestId("retirement-success").textContent).toMatch(/^\d+\.\d%$/);
    // …and the timeline re-keys to the shorter horizon (25 rows), no throw.
    expect(timelineRows()).toHaveLength(PLAN_END_AGE - 70);
    vi.useRealTimers();
  });
});

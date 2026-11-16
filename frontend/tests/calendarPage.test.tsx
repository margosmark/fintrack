// M52 cash-flow calendar page (docs/36): zero-filled month grid, kind dots,
// the day popover, click-through to each event's source, month navigation and
// the server-provided `today` marker.
// The calendar page tests pull the token block straight from the stylesheet.
import { readFileSync } from "node:fs";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../src/api/client";
import { Calendar } from "../src/pages/Calendar";
import type { CalendarEvent } from "../src/api/cashflowCalendar";

const mocks = vi.hoisted(() => ({ getCashflowCalendar: vi.fn() }));

vi.mock("../src/api/cashflowCalendar", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/cashflowCalendar")>();
  return { ...actual, getCashflowCalendar: mocks.getCashflowCalendar };
});

function daysOfMonth(month: string, byDate: Record<string, CalendarEvent[]> = {}) {
  const [year, mon] = month.split("-").map(Number);
  const lastDay = new Date(year, mon, 0).getDate();
  return Array.from({ length: lastDay }, (_, index) => {
    const date = `${month}-${String(index + 1).padStart(2, "0")}`;
    return { date, events: byDate[date] ?? [] };
  });
}

function response(
  month: string,
  byDate: Record<string, CalendarEvent[]> = {},
  today = `${month}-10`,
  confirmed_subscription_count = 0,
) {
  return { month, today, days: daysOfMonth(month, byDate), confirmed_subscription_count };
}

const BILL: CalendarEvent = {
  kind: "bill",
  label: "Netflix",
  amount: "-16.99",
  currency: "CAD",
  source_id: "7",
  status: "past",
};
const DIVIDEND: CalendarEvent = {
  kind: "dividend",
  label: "RY dividend",
  amount: "30.00",
  currency: "CAD",
  source_id: "3",
  status: "upcoming",
};
const SUBSCRIPTION: CalendarEvent = {
  kind: "subscription",
  label: "NETFLIX.COM",
  amount: "-15.99",
  currency: "CAD",
  source_id: "NETFLIX.COM",
  status: "today",
};
/** M170 follow-up: a group whose rows are USD — the figure is stated in USD. */
const USD_SUBSCRIPTION: CalendarEvent = {
  kind: "subscription",
  label: "SPOTIFY",
  amount: "-11.99",
  currency: "USD",
  source_id: "SPOTIFY",
  status: "today",
};
/** M170 follow-up: mixed / unknown currency — no honest figure at all. */
const UNSTATED_SUBSCRIPTION: CalendarEvent = {
  kind: "subscription",
  label: "DROPBOX",
  amount: null,
  currency: null,
  source_id: "DROPBOX",
  status: "today",
};

/** Always-rendered location probe, so click-through navigation is observable. */
function RouteProbe() {
  const location = useLocation();
  return <div data-testid="route-probe">{`${location.pathname}${location.search}`}</div>;
}

function renderCalendar(initialEntries: string[] = ["/calendar"]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <QueryClientProvider client={client}>
        <RouteProbe />
        <Routes>
          <Route path="/calendar" element={<Calendar />} />
          <Route path="/bills" element={<div>bills page</div>} />
          <Route path="/securities/:securityId" element={<div>security page</div>} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function dayCells(container: HTMLElement) {
  return [...container.querySelectorAll("[data-testid^='calendar-day-']")];
}

beforeEach(() => {
  mocks.getCashflowCalendar.mockReset();
  mocks.getCashflowCalendar.mockImplementation(async (month?: string) =>
    response(month ?? "2026-02"),
  );
});

describe("cash-flow calendar grid", () => {
  it("zero-fills every day of the month with no error for an empty month", async () => {
    const { container } = renderCalendar();

    await waitFor(() =>
      expect(screen.getByTestId("calendar-month").textContent ?? "").toContain("February 2026"),
    );

    // 28 day buttons, each present and empty — the grid is never replaced by
    // an error/empty panel (docs/36 decision 3).
    expect(dayCells(container)).toHaveLength(28);
    expect(screen.queryByText("Something went wrong")).toBeNull();
    expect(screen.getByText("No cash-flow events in February 2026.")).toBeTruthy();
    expect(screen.queryByTestId("calendar-popover")).toBeNull();
  });

  it("fetches the server's current month first (the client never invents today)", async () => {
    renderCalendar();
    await waitFor(() => expect(mocks.getCashflowCalendar).toHaveBeenCalledWith(undefined));
  });

  it("marks the server-provided today with aria-current=date", async () => {
    const { container } = renderCalendar();
    await waitFor(() =>
      expect(screen.getByTestId("calendar-day-2026-02-10").getAttribute("aria-current")).toBe(
        "date",
      ),
    );
    expect(screen.getByTestId("calendar-day-2026-02-11").getAttribute("aria-current")).toBeNull();
    expect(dayCells(container)).toHaveLength(28);
  });

  it("draws one dot per event kind on the day cell", async () => {
    mocks.getCashflowCalendar.mockImplementation(async (month?: string) =>
      response(month ?? "2026-02", { "2026-02-10": [BILL, DIVIDEND, SUBSCRIPTION] }),
    );
    const { container } = renderCalendar();
    const cell = await screen.findByTestId("calendar-day-2026-02-10");
    const kinds = [...cell.querySelectorAll("[data-kind]")].map((dot) =>
      dot.getAttribute("data-kind"),
    );
    expect(kinds).toEqual(["bill", "dividend", "subscription"]);
    // M67: the dots stay on SEMANTIC tokens — amber bill / green dividend /
    // indigo subscription — and never follow the categorical chart palette.
    const expected: Record<string, string> = {
      bill: "bg-warn",
      dividend: "bg-gain",
      subscription: "bg-accent",
    };
    for (const dot of cell.querySelectorAll("[data-kind]")) {
      const kind = dot.getAttribute("data-kind") ?? "";
      expect(
        dot.classList.contains(expected[kind]),
        `${kind} dot classes: ${dot.className}`,
      ).toBe(true);
    }
    // days without events carry no dots
    expect(screen.getByTestId("calendar-day-2026-02-11").querySelectorAll("[data-kind]")).toHaveLength(0);
    expect(container).toBeTruthy();
  });

  it("keeps each dot token in its documented colour family (both themes)", () => {
    // Token source of truth: `:root` / `:root:not(.dark)` in src/index.css.
    const css = readFileSync("src/index.css", "utf8");
    const darkStart = css.indexOf("  :root {");
    const lightStart = css.indexOf("  :root:not(.dark) {");
    const lightEnd = css.indexOf("}", css.indexOf("--ambient-c", lightStart));
    const blocks = {
      dark: css.slice(darkStart, lightStart),
      light: css.slice(lightStart, lightEnd),
    };

    const hue = (block: string, name: string): number => {
      const match = block.match(new RegExp(`--color-${name}:\\s*(\\d+) (\\d+) (\\d+);`));
      expect(match, `--color-${name} missing`).not.toBeNull();
      const [r, g, b] = [1, 2, 3].map((i) => Number(match![i]));
      const max = Math.max(r, g, b);
      const min = Math.min(r, g, b);
      const d = max - min;
      if (d === 0) return 0;
      const h =
        max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
      return (h * 60 + 360) % 360;
    };

    // bill = amber, dividend = green in both themes…
    expect(hue(blocks.dark, "warn")).toBeGreaterThanOrEqual(25);
    expect(hue(blocks.dark, "warn")).toBeLessThanOrEqual(50);
    expect(hue(blocks.light, "warn")).toBeGreaterThanOrEqual(20);
    expect(hue(blocks.light, "warn")).toBeLessThanOrEqual(50);
    expect(hue(blocks.dark, "gain")).toBeGreaterThanOrEqual(130);
    expect(hue(blocks.dark, "gain")).toBeLessThanOrEqual(175);
    expect(hue(blocks.light, "gain")).toBeGreaterThanOrEqual(130);
    expect(hue(blocks.light, "gain")).toBeLessThanOrEqual(175);
    // …subscription is signal blue in BOTH themes since the M185 Ledger
    // re-skin (docs/47 §5: the accent is one signal-blue family, polarity by
    // theme; the light theme still carries its OWN darker lead, never the
    // dark theme's tint — hue 216.4 vs 218.5, separated by luminance).
    expect(hue(blocks.light, "accent")).toBeGreaterThanOrEqual(205);
    expect(hue(blocks.light, "accent")).toBeLessThanOrEqual(230);
    expect(hue(blocks.dark, "accent")).toBeGreaterThanOrEqual(200);
    expect(hue(blocks.dark, "accent")).toBeLessThanOrEqual(230);
  });

  it("opens a day popover listing label, signed amount and status, and closes on Escape", async () => {
    mocks.getCashflowCalendar.mockImplementation(async (month?: string) =>
      response(month ?? "2026-02", { "2026-02-10": [BILL, DIVIDEND, SUBSCRIPTION] }),
    );
    renderCalendar();
    const cell = await screen.findByTestId("calendar-day-2026-02-10");

    fireEvent.click(cell);
    const popover = await screen.findByTestId("calendar-popover");
    expect(popover.getAttribute("role")).toBe("dialog");
    expect(popover.getAttribute("aria-label")).toBe("Tuesday, February 10, 2026");
    expect(screen.getByText("Netflix")).toBeTruthy();
    expect(screen.getByText("-$16.99")).toBeTruthy();
    expect(screen.getByText("RY dividend")).toBeTruthy();
    expect(screen.getByText("$30.00")).toBeTruthy();
    expect(screen.getByText("NETFLIX.COM")).toBeTruthy();
    expect(screen.getByText("past")).toBeTruthy();
    expect(screen.getByText("today")).toBeTruthy();
    expect(screen.getByText("upcoming")).toBeTruthy();

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("calendar-popover")).toBeNull());
  });

  it("shows the empty-day line when the selected day has no events", async () => {
    renderCalendar();
    const cell = await screen.findByTestId("calendar-day-2026-02-28");

    fireEvent.click(cell);
    expect(await screen.findByTestId("calendar-popover")).toBeTruthy();
    expect(screen.getByText("No cash-flow events")).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("calendar-popover")).toBeNull());
  });
});

describe("click-through to source pages", () => {
  beforeEach(() => {
    mocks.getCashflowCalendar.mockImplementation(async (month?: string) =>
      response(month ?? "2026-02", { "2026-02-10": [BILL, DIVIDEND, SUBSCRIPTION] }),
    );
  });

  it("takes a bill event to the Bills page", async () => {
    renderCalendar();
    const cell = await screen.findByTestId("calendar-day-2026-02-10");
    fireEvent.click(cell);
    await screen.findByTestId("calendar-popover");

    // a bill's target is the Bills page itself (docs/36 decision 8)
    fireEvent.click(screen.getByRole("link", { name: /Netflix/ }));
    await waitFor(() =>
      expect(screen.getByTestId("route-probe").textContent ?? "").toBe("/bills"),
    );
  });

  it("takes a dividend to its security page", async () => {
    renderCalendar();
    const cell = await screen.findByTestId("calendar-day-2026-02-10");
    fireEvent.click(cell);
    await screen.findByTestId("calendar-popover");

    fireEvent.click(screen.getByRole("link", { name: /RY dividend/ }));
    await waitFor(() =>
      expect(screen.getByTestId("route-probe").textContent ?? "").toContain("/securities/3"),
    );
  });

  it("takes a subscription to the M55 bill draft hand-off", async () => {
    renderCalendar();
    const cell = await screen.findByTestId("calendar-day-2026-02-10");
    fireEvent.click(cell);
    await screen.findByTestId("calendar-popover");

    fireEvent.click(screen.getByRole("link", { name: /NETFLIX.COM/ }));
    await waitFor(() =>
      expect(screen.getByTestId("route-probe").textContent).toContain("/bills?autofill=NETFLIX.COM"),
    );
    const probe = screen.getByTestId("route-probe").textContent ?? "";
    expect(probe).toContain("amount=15.99");
    expect(probe).toContain("due=2026-02-10");
  });
});

describe("month navigation", () => {
  it("passes the shifted month forward and resets to the default on Today", async () => {
    renderCalendar();
    await waitFor(() => expect(mocks.getCashflowCalendar).toHaveBeenCalledWith(undefined));
    await screen.findByTestId("calendar-day-2026-02-01");

    fireEvent.click(screen.getByLabelText("Next month"));
    await waitFor(() => expect(mocks.getCashflowCalendar).toHaveBeenCalledWith("2026-03"));
    expect((await screen.findByTestId("calendar-month")).textContent ?? "").toContain(
      "March 2026",
    );

    fireEvent.click(screen.getByLabelText("Previous month"));
    fireEvent.click(screen.getByLabelText("Previous month"));
    await waitFor(() => expect(mocks.getCashflowCalendar).toHaveBeenCalledWith("2026-01"));

    fireEvent.click(screen.getByRole("button", { name: "Today" }));
    await waitFor(() => expect(mocks.getCashflowCalendar).toHaveBeenCalledWith(undefined));
  });

  it("surfaces an API failure as the shared error panel", async () => {
    mocks.getCashflowCalendar.mockRejectedValueOnce(
      new ApiError(422, "Invalid month: '2026-13' — expected YYYY-MM, year 1900-2200", "invalid_month"),
    );
    renderCalendar();
    expect(await screen.findByText("Something went wrong")).toBeTruthy();
    expect(screen.getByText("Invalid month: '2026-13' — expected YYYY-MM, year 1900-2200")).toBeTruthy();
  });
});

// M170 (docs/36 §M170): the subscription series is CONFIRMED state, so an
// empty one is a fact worth saying out loud — and the response's
// `confirmed_subscription_count` is what tells the two honest sentences apart.
describe("M170 confirmed-subscriptions empty state", () => {
  it("says 'nothing confirmed yet' when the confirmed set is empty", async () => {
    mocks.getCashflowCalendar.mockImplementation(async (month?: string) =>
      response(month ?? "2026-02", { "2026-02-10": [BILL] }, `${month ?? "2026-02"}-10`, 0),
    );
    renderCalendar();
    const line = await screen.findByTestId("calendar-subscriptions-empty");
    expect(line.textContent).toContain("No confirmed subscriptions yet");
    expect(line.textContent).toContain("/transactions");
  });

  it("says 'none this month' when the user HAS confirmed subscriptions", async () => {
    mocks.getCashflowCalendar.mockImplementation(async (month?: string) =>
      response(month ?? "2026-02", { "2026-02-10": [BILL] }, `${month ?? "2026-02"}-10`, 5),
    );
    renderCalendar();
    const line = await screen.findByTestId("calendar-subscriptions-empty");
    expect(line.textContent).toContain("No subscription payments in February 2026");
    expect(line.textContent).toContain("confirmed");
  });

  it("says nothing at all when the month shows a confirmed subscription", async () => {
    mocks.getCashflowCalendar.mockImplementation(async (month?: string) =>
      response(month ?? "2026-02", { "2026-02-10": [SUBSCRIPTION] }, `${month ?? "2026-02"}-10`, 1),
    );
    renderCalendar();
    await screen.findByTestId("calendar-day-2026-02-10");
    expect(screen.queryByTestId("calendar-subscriptions-empty")).toBeNull();
  });
});

// M170 follow-up (docs/36 §Currency): the amount renders in the currency the
// event was RECORDED in. A native figure is never printed as CAD (`US$` vs
// `$` is the whole point), and a mixed/unknown group shows the app's "no
// honest figure" dash instead of a number nobody computed.
describe("M170 follow-up currency honesty", () => {
  async function openDay(events: CalendarEvent[]) {
    mocks.getCashflowCalendar.mockImplementation(async (month?: string) =>
      response(month ?? "2026-02", { "2026-02-10": events }, `${month ?? "2026-02"}-10`, 1),
    );
    renderCalendar();
    const cell = await screen.findByTestId("calendar-day-2026-02-10");
    fireEvent.click(cell);
    return screen.findByTestId("calendar-popover");
  }

  it("renders a USD subscription in USD — never as a CAD figure", async () => {
    await openDay([USD_SUBSCRIPTION]);

    expect(screen.getByText("SPOTIFY")).toBeTruthy();
    // en-CA renders USD with the `US$` marker, so the two cannot be confused.
    expect(screen.getByText("-US$11.99")).toBeTruthy();
    expect(screen.queryByText("-$11.99")).toBeNull();
  });

  it("keeps the CAD bill and dividend controls in CAD", async () => {
    await openDay([BILL, DIVIDEND]);

    expect(screen.getByText("-$16.99")).toBeTruthy();
    expect(screen.getByText("$30.00")).toBeTruthy();
  });

  it("shows the no-honest-figure dash for a mixed/unknown-currency group", async () => {
    await openDay([UNSTATED_SUBSCRIPTION]);

    // The commitment still renders (it has a date); only its amount is
    // withheld — `formatMoney(null)` is a dash, never a fabricated $0.00.
    expect(screen.getByText("DROPBOX")).toBeTruthy();
    expect(screen.getByText("—")).toBeTruthy();
  });

  it("omits the amount param when the figure is not CAD", async () => {
    await openDay([USD_SUBSCRIPTION]);

    fireEvent.click(screen.getByRole("link", { name: /SPOTIFY/ }));
    await waitFor(() =>
      expect(screen.getByTestId("route-probe").textContent ?? "").toContain(
        "/bills?autofill=SPOTIFY",
      ),
    );
    const probe = screen.getByTestId("route-probe").textContent ?? "";
    // A bill is CAD by schema (docs/25): prefilling it with a USD number
    // would create a wrong bill, so the amount param is omitted entirely.
    expect(probe).not.toContain("amount=");
    expect(probe).toContain("due=2026-02-10");
  });

  it("omits the amount param for an unstated figure too", async () => {
    await openDay([UNSTATED_SUBSCRIPTION]);

    fireEvent.click(screen.getByRole("link", { name: /DROPBOX/ }));
    await waitFor(() =>
      expect(screen.getByTestId("route-probe").textContent ?? "").toContain(
        "/bills?autofill=DROPBOX",
      ),
    );
    expect(screen.getByTestId("route-probe").textContent ?? "").not.toContain("amount=");
  });
});

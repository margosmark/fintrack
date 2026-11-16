// The calendar/display-mask failure-mode matrix, end to end (t_742c9dc0).
//
// Three failure modes are named on the card — month-end, currency,
// restore-null. This file owns the two that are client-side and JOINS to the
// server half through one payload instead of a hand-written fixture:
//
//   server half — `backend/tests/test_calendar_failure_modes.py` walks a
//   31st-anchored all-USD group through the REAL route across Feb/Mar/Apr 2027
//   and byte-diffs the Feb answer against
//   `tests/fixtures/calendar-failure-modes.json`;
//   THIS file renders that exact captured payload. A contract drift fails the
//   side that stopped producing or reading it; a render/mask regression fails
//   here; a month-end or currency regression fails there.
//
// What is deliberately NOT mocked: `../src/api/client`. The boot probe and the
// login run against a `fetch` stub at the NETWORK boundary, which is the shape
// c265cdd turned on — `postRefreshToken` RESOLVES null on a non-ok status and
// on a thrown fetch, it never rejects, so a test that mocks `restoreSession`
// can only ever exercise the `.catch` that production never reaches
// (m183FailedBootMask.test.tsx proved that seam was jsdom-green, prod-red).
//
// Rows:
//   R1  month-end × currency, unmasked — the Feb grid is 28 cells, the
//       31st-anchored group's event sits on the 28th, and its figure prints
//       in ITS OWN currency (`-US$11.99`), never as CAD.
//   R2  the mask flip on that same page — every figure becomes `•••••`, no
//       amount-shaped token survives ANYWHERE in the document (the F-1 class:
//       a surface that keeps printing while the live region says masked), and
//       the flip costs zero refetches (docs/46 D3/D6).
//   R3  restore-null → demo — a stale `fintrack.mask="1"` outlives a dead
//       session, the refresh probe answers 401 (resolves, does not throw), the
//       boot contract clears the key, the demo door applies the read-only
//       default, and the calendar the user lands on is masked.
//   R4  restore-null → ordinary sign-in — the same stale key must NOT survive
//       into the new identity: the calendar prints real figures.
//   R5  a SUCCESSFUL restore (fef0ac9) — the boot contract runs on the
//       restore path too, so a key written by a dead session does not travel
//       into the new one and the calendar starts in the clear.
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { router } from "../src/App";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { DEMO_EMAIL } from "../src/auth/readOnlySession";
import {
  MASK_SESSION_KEY,
  initMaskForSession,
  isMasked,
  setMasked,
} from "../src/lib/mask";
import type { User } from "../src/types";

/** The payload the SERVER half byte-diffs against the live route. */
const GOLDEN = JSON.parse(readFileSync("tests/fixtures/calendar-failure-modes.json", "utf8")) as {
  cases: {
    name: string;
    request: { month: string };
    response: {
      month: string;
      today: string;
      days: {
        date: string;
        events: {
          kind: string;
          label: string;
          amount: string | null;
          currency: string | null;
          source_id: string;
          status: string;
        }[];
      }[];
      confirmed_subscription_count: number;
    };
  }[];
};
const CASE = GOLDEN.cases.find((entry) => entry.name === "month_end_usd_feb");
if (!CASE) throw new Error("calendar-failure-modes.json has no month_end_usd_feb case");

/** The month that lacks the 31st, and the day the rule puts the group on. */
const MONTH = CASE.request.month;
const EVENT_DAY = "2027-02-28";
/** en-CA currency formatting of `-11.99` in USD (docs/36 §Currency). */
const USD_FIGURE = "-US$11.99";

const mocks = vi.hoisted(() => ({
  getCashflowCalendar: vi.fn(),
  listAccounts: vi.fn(),
  listCategories: vi.fn(),
  getNotificationInbox: vi.fn(),
  markNotificationRead: vi.fn(),
  markAllNotificationsRead: vi.fn(),
  unreadBadgeLabel: vi.fn(),
}));

vi.mock("../src/api/cashflowCalendar", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/cashflowCalendar")>();
  return { ...actual, getCashflowCalendar: mocks.getCashflowCalendar };
});
vi.mock("../src/api/accounts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/accounts")>();
  return { ...actual, listAccounts: mocks.listAccounts };
});
vi.mock("../src/api/categories", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/categories")>();
  return { ...actual, listCategories: mocks.listCategories };
});
vi.mock("../src/api/notifications", () => ({
  INBOX_QUERY_KEY: ["notifications", "inbox"],
  getNotificationInbox: mocks.getNotificationInbox,
  markNotificationRead: mocks.markNotificationRead,
  markAllNotificationsRead: mocks.markAllNotificationsRead,
  unreadBadgeLabel: mocks.unreadBadgeLabel,
}));

/** A `Response` the real client accepts, without a real network. */
function response(body: unknown, status = 200): Response {
  return {
    ok: status < 400,
    status,
    statusText: status === 401 ? "Unauthorized" : "OK",
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

const NORMAL_USER: User = {
  id: "user-1",
  email: "mark@example.com",
  privacy_mode: false,
  currency_tracker_enabled: false,
  llm_provider: null,
  llm_base_url: null,
  llm_model: null,
  encryption_required: false,
  dashboard_preset: "overview",
} as User;

const DEMO_USER: User = { ...NORMAL_USER, id: "demo-1", email: DEMO_EMAIL } as User;

/** Every endpoint the real client can reach in this flow, plus a tripwire. */
const fetchLog: string[] = [];

type Boot = "failed" | "restored";

function installFetch(boot: Boot): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input instanceof Request ? input.url : input);
      fetchLog.push(url);
      if (url.endsWith("/auth/refresh")) {
        // The failure shape c265cdd exists for: a non-ok answer the client
        // turns into a RESOLVED null (never a rejection).
        return boot === "failed"
          ? response({ detail: "refresh token expired" }, 401)
          : response({
              access_token: "access",
              token_type: "bearer",
              user: NORMAL_USER,
            });
      }
      // The demo door posts to `/auth/demo` (client.ts `demoLogin`).
      if (url.endsWith("/auth/demo")) {
        return response({ access_token: "access", token_type: "bearer", user: DEMO_USER });
      }
      if (url.endsWith("/auth/login")) {
        return response({ access_token: "access", token_type: "bearer", user: NORMAL_USER });
      }
      return response({ detail: `not stubbed: ${url}` }, 404);
    }),
  );
}

/** Everything money-shaped currently in the document (the mask's whole job). */
function amountTokens(root: ParentNode = document): string[] {
  const text = root.textContent ?? "";
  return text.match(/\d{1,3}(?:,\d{3})*\.\d{2}/g) ?? [];
}

/**
 * Every currency figure the popover prints, in order.
 *
 * The row is one link of four spans — dot, label, figure, status — so the
 * FIGURE is read by position rather than by matching the expected string
 * (which would pass on a row that printed the figure inside its label).
 */
function popoverFigures(): string[] {
  const popover = screen.getByTestId("calendar-popover");
  return [...popover.querySelectorAll("li a")].map((row) => {
    const spans = row.querySelectorAll(":scope > span");
    return spans[2]?.textContent?.trim() ?? "";
  });
}

async function renderApp(): Promise<void> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnMount: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <App />
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

/** Land on `/calendar` the way a deep link does, before the boot probe runs. */
async function goToCalendar(): Promise<void> {
  await act(async () => {
    await router.navigate("/calendar", { replace: true });
  });
}

/** Open the day that carries the 31st-anchored group's event. */
async function openEventDay(): Promise<void> {
  const cell = await screen.findByTestId(`calendar-day-${EVENT_DAY}`);
  await waitFor(() => expect(cell.getAttribute("aria-label")).toContain("1 event"));
  fireEvent.click(cell);
  await screen.findByTestId("calendar-popover");
}

/** jsdom has no matchMedia; same query-aware stub the other shell tests use. */
function installMatchMedia(): void {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: (query: string) => {
      const min = /min-width:\s*(\d+)px/.exec(query);
      return {
        matches: min !== null ? window.innerWidth >= Number(min[1]) : false,
        media: query,
        onchange: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      };
    },
  });
}
installMatchMedia();

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
  fetchLog.length = 0;
  mocks.getCashflowCalendar.mockResolvedValue(CASE.response);
  mocks.listAccounts.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  mocks.listCategories.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  mocks.getNotificationInbox.mockResolvedValue({ items: [], unread_count: 0 });
  mocks.markNotificationRead.mockResolvedValue(undefined);
  mocks.markAllNotificationsRead.mockResolvedValue(undefined);
  mocks.unreadBadgeLabel.mockReturnValue("");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  sessionStorage.clear();
  initMaskForSession();
});

describe("R1 — month-end × currency, unmasked (the captured payload, rendered)", () => {
  it("draws a 28-cell February with the group on the 28th, stating USD", async () => {
    // The fixture's request and its captured answer agree on the month — a
    // golden captured against a different `?month=` would be worse than none.
    expect(CASE.response.month).toBe(MONTH);
    installFetch("restored");
    await goToCalendar();
    await renderApp();

    const cells = await screen.findAllByTestId(/^calendar-day-/);
    expect(cells).toHaveLength(28);

    const eventCell = screen.getByTestId(`calendar-day-${EVENT_DAY}`);
    expect(eventCell.getAttribute("aria-label")).toContain("1 event");

    await openEventDay();
    // The popover is the only place the figure is printed (docs/36): label,
    // figure, status — and the figure is in the row's OWN currency.
    expect(screen.getByText("MATRIX FAILURE USD")).toBeTruthy();
    expect(popoverFigures()).toEqual([USD_FIGURE]);
    expect(screen.getByText("upcoming")).toBeTruthy();

    // Not vacuous: the client really did ask for this month's grid.
    expect(mocks.getCashflowCalendar).toHaveBeenCalledTimes(1);
    expect(fetchLog.filter((url) => url.includes("/auth/refresh"))).toHaveLength(1);
  });
});

describe("R2 — the mask flip on that page (docs/46 D2/D3/D6)", () => {
  it("hides every figure, leaves no amount-shaped token, and refetches nothing", async () => {
    installFetch("restored");
    await goToCalendar();
    await renderApp();
    await openEventDay();
    expect(popoverFigures()).toEqual([USD_FIGURE]);

    act(() => setMasked(true));

    // The row keeps its label, status and position; only the figure masks.
    expect(screen.getByText("MATRIX FAILURE USD")).toBeTruthy();
    expect(screen.getByText("upcoming")).toBeTruthy();
    expect(popoverFigures()).toEqual(["•••••"]);
    // The F-1 class, asserted over the WHOLE document: a masked session must
    // not print a 2-decimal amount anywhere, whatever surface it came from.
    expect(amountTokens()).toEqual([]);
    expect(isMasked()).toBe(true);

    act(() => setMasked(false));
    expect(popoverFigures()).toEqual([USD_FIGURE]);
    // D3: flipping is one store write — zero network traffic either way.
    expect(mocks.getCashflowCalendar).toHaveBeenCalledTimes(1);
    expect(fetchLog.filter((url) => url.includes("/auth/"))).toHaveLength(1);
  });
});

describe("R3 — restore-null → the read-only demo door", () => {
  it("clears the stale key on the failed probe and lands on a masked calendar", async () => {
    // The state the defect leaves behind: a masked session died in this tab,
    // and the key it wrote is still there when the reload's probe fails.
    sessionStorage.setItem(MASK_SESSION_KEY, "1");
    installFetch("failed");
    await goToCalendar();
    await renderApp();

    // The login page is where a failed boot lands (RequireAuth → /login).
    const demoDoor = await screen.findByRole("button", { name: /explore the demo/i });
    // c265cdd/c3135f5: the probe RESOLVES null, and the failed boot is still a
    // boot — the stale key is gone before anyone signs in.
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
    expect(isMasked()).toBe(false);

    fireEvent.click(demoDoor);

    // The demo tenant takes the read-only default (docs/46 D1), so the calendar
    // the user reaches is masked — the contract, seen on a money surface.
    await openEventDay();
    expect(isMasked()).toBe(true);
    expect(popoverFigures()).toEqual(["•••••"]);
    expect(amountTokens()).toEqual([]);
    // The a11y half of the same claim: the live region says masked, and the
    // DOM now agrees with it (F-1 was a live region announcing the mask while
    // the prose beside it printed real figures).
    expect(screen.getByTestId("mask-live-region").textContent).toContain("Amounts masked");
  });
});

describe("R5 — a RESTORED session (fef0ac9): the boot contract runs there too", () => {
  it("clears the dead session's mask key on a successful restore", async () => {
    // The other half of the same seam: the probe SUCCEEDS, which is the path
    // that used to seed the cache owner and skip the mask contract entirely
    // (m139 pinned it on the shell's rail; this pins it on a money surface,
    // with the payload the server half byte-diffs).
    sessionStorage.setItem(MASK_SESSION_KEY, "1");
    installFetch("restored");
    await goToCalendar();
    await renderApp();

    await openEventDay();
    // An ordinary (non-demo) session starts unmasked: restore-on-load is OFF
    // by PM ruling (docs/46 D1), so a key from a PREVIOUS session is stale
    // state, not a preference that travelled with the cookie.
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
    expect(isMasked()).toBe(false);
    expect(popoverFigures()).toEqual([USD_FIGURE]);
    expect(amountTokens()).toEqual([]);
  });
});

describe("R4 — restore-null → an ordinary sign-in", () => {
  it("does not carry the dead session's mask into the next identity", async () => {
    sessionStorage.setItem(MASK_SESSION_KEY, "1");
    installFetch("failed");
    await goToCalendar();
    await renderApp();

    // No demo door: sign in through the form, which is the explicit transition
    // `applyUser` owns (mask dies with the identity it belonged to).
    // The sign-in form labels its two fields by PLACEHOLDER (Login.tsx), and
    // the TOTP step is the one with a real <label> — match what ships.
    const email = await screen.findByPlaceholderText(/email/i);
    fireEvent.change(email, { target: { value: "mark@example.com" } });
    const password = screen.getByPlaceholderText(/password/i);
    fireEvent.change(password, { target: { value: "correct-horse-battery" } });
    const form = password.closest("form");
    expect(form).toBeTruthy();
    fireEvent.submit(form as HTMLFormElement);

    await openEventDay();
    expect(isMasked()).toBe(false);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
    expect(popoverFigures()).toEqual([USD_FIGURE]);
  });
});

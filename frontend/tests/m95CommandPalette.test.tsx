// M95 P1 — the ⌘K / Ctrl+K command palette (docs/09 "Command palette").
//
// Acceptance items the card names, one describe each:
//   1. registry — existing routes only, plain case-insensitive match, the
//      empty query's top 8 are exactly the seven navigate-to pages (M100
//      added Accounts) + the Import action, no fuzz/typo layer;
//   2. open / navigate / close cycle — trigger → typed query → Enter lands on
//      the route, the palette closes, and focus HANDS OFF to the routed page
//      (docs/09 route-focus handoff — the opener restore would park a
//      keyboard user back in the chrome they just left). A close that does
//      NOT navigate still returns focus to its opener.
//   3. Escape ordering — the palette is the TOP-most consumer of the shell's
//      single Escape router: press 1 closes only the palette, press 2 reaches
//      the drawer behind it (and a fresh keydown listener is never added);
//   4. mobile trigger — the header magnifier exists below md at the 44px
//      floor with `aria-haspopup`/`aria-expanded`;
//   5. zero console errors on every invocation;
//   6. honest empty results copy, and the "Take the tour" action resurfaces
//      the tour through its own event instead of navigating.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { searchGlobal } from "../src/api/search";
import { lockVault } from "../src/lib/vault";
import { AppShell } from "../src/components/layout/AppShell";
import {
  matchPaletteEntries,
  paletteEntries,
} from "../src/components/layout/CommandPalette";

vi.mock("../src/api/accounts", () => ({
  listAccounts: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 }),
}));

vi.mock("../src/api/search", () => ({ searchGlobal: vi.fn() }));

vi.mock("../src/api/notifications", () => ({
  INBOX_QUERY_KEY: ["notifications", "inbox"],
  getNotificationInbox: vi.fn().mockResolvedValue({ items: [], unread_count: 0 }),
  markNotificationRead: vi.fn().mockResolvedValue(undefined),
  markAllNotificationsRead: vi.fn().mockResolvedValue(undefined),
  unreadBadgeLabel: () => "",
}));

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({ user: authState.user, logout: vi.fn() }),
}));

vi.mock("../src/theme/ThemeProvider", () => ({
  useTheme: () => ({ theme: "dark", toggleTheme: vi.fn() }),
}));

vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

/** True while the emulated viewport is >= 768px (the shell's breakpoint). */
let desktopViewport = false;

/**
 * The AppShell's auth state under this file's mock. `vi.hoisted` so the hoisted
 * `vi.mock` factory above can read it; every test that needs the shell signed
 * out flips this and restores the signed-in default afterwards.
 */
const authState = vi.hoisted(() => ({
  user: { id: "test-user", email: "test@example.com" } as
    | { id: string; email: string }
    | null,
}));

Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: /min-width/.test(query) ? desktopViewport : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

/** Renders the current location so navigation is assertable. */
function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location">
      {location.pathname}
      {location.search}
    </div>
  );
}

function renderShell(initialPath = "/dashboard", retry: boolean | number = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry, staleTime: Infinity } },
  });
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <QueryClientProvider client={client}>
        <LocationProbe />
        <AppShell />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const palette = () => screen.queryByTestId("command-palette");
const input = () => screen.queryByTestId("command-palette-input") as HTMLInputElement | null;
const location = () => screen.getByTestId("location").textContent ?? "";

async function openPalette(triggerTestId: string): Promise<HTMLInputElement> {
  const trigger = await screen.findByTestId(triggerTestId);
  // A real pointer press focuses the button before the click lands (Chrome
  // focuses on mousedown) — dialogA11y records THAT as the opener to hand
  // focus back to, so reproduce it here instead of dispatching a focus-less
  // synthetic click.
  trigger.focus();
  fireEvent.click(trigger);
  await waitFor(() => expect(palette()).not.toBeNull());
  const field = input();
  expect(field).not.toBeNull();
  return field as HTMLInputElement;
}

beforeEach(() => {
  desktopViewport = false;
  lockVault();
  // The coach marks teach the UI and never auto-fire for an account that has
  // already seen them — keep them out of the way of every assertion here.
  window.localStorage.setItem("fin_tracker.tour.v1.test-user", "completed");
});

afterEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("command palette registry (docs/09 §Command palette)", () => {
  it("wires only routes the app already serves", () => {
    const known = new Set([
      "/transactions",
      "/budgets",
      "/reports",
      "/goals",
      "/settings",
      "/help",
      "/imports",
      "/help/agent-integration",
      // M100: the accounts index.
      "/accounts",
    ]);
    const reportTabs = ["spending", "categories", "cashflow", "networth", "performance", "dividends", "projections"];
    for (const entry of paletteEntries()) {
      if (entry.to === undefined) continue;
      if (entry.to.startsWith("/reports?tab=")) {
        expect(reportTabs).toContain(entry.to.replace("/reports?tab=", ""));
        continue;
      }
      if (entry.to.startsWith("/help/")) {
        expect(entry.to.length).toBeGreaterThan("/help/".length);
        continue;
      }
      expect(known.has(entry.to)).toBe(true);
    }
    // Exactly one entry has no route: the tour, which resurfaces in place.
    expect(paletteEntries().filter((entry) => entry.to === undefined)).toHaveLength(1);
  });

  it("shows the top 8 in registry order on an empty query", () => {
    const top = matchPaletteEntries(paletteEntries(), "");
    expect(top).toHaveLength(8);
    // M100 added `page-accounts` last among pages, so the eighth slot is now
    // the Import action and "Take the tour" sits at #9 — every pre-M100 entry
    // keeps the rank it had.
    expect(top.map((entry) => entry.id)).toEqual([
      "page-transactions",
      "page-budgets",
      "page-reports",
      "page-goals",
      "page-settings",
      "page-help",
      "page-accounts",
      "action-import",
    ]);
  });

  it("matches case-insensitively, prefix before substring, no fuzz", () => {
    const entries = paletteEntries();
    expect(matchPaletteEntries(entries, "BUD").map((e) => e.id)).toContain("page-budgets");
    // "reports" prefix-matches the Reports page; "report" also reaches the
    // report-tab entries through their keyword haystack.
    expect(matchPaletteEntries(entries, "reports")[0]?.id).toBe("page-reports");
    expect(matchPaletteEntries(entries, "dividends")[0]?.id).toBe("report-dividends");
    // Plain substring, deliberately not fuzzy: a typo matches nothing.
    expect(matchPaletteEntries(entries, "budgetsx")).toHaveLength(0);
  });

  it("lists all seven report tabs and the guide walkthroughs", () => {
    const ids = paletteEntries().map((entry) => entry.id);
    for (const tab of ["spending", "categories", "cashflow", "networth", "performance", "dividends", "projections"]) {
      expect(ids).toContain(`report-${tab}`);
    }
    expect(ids).toContain("guide-first-import");
    expect(ids).toContain("guide-agent-integration");
  });
});

describe("open / navigate / close cycle", () => {
  it("opens from the header trigger, focuses the input, goes, closes — focus hands off to the page", async () => {
    renderShell();
    const field = await openPalette("command-palette-trigger-mobile");
    await waitFor(() => expect(document.activeElement).toBe(field));
    expect(field.getAttribute("role")).toBe("combobox");
    expect(field.getAttribute("aria-expanded")).toBe("true");

    fireEvent.change(field, { target: { value: "bud" } });
    await waitFor(() =>
      expect(within(screen.getByTestId("command-palette-listbox")).getByText("Budgets")).toBeTruthy(),
    );
    fireEvent.keyDown(field, { key: "Enter" });

    await waitFor(() => expect(location()).toBe("/budgets"));
    await waitFor(() => expect(palette()).toBeNull());
    // The route changed, so focus lands in the routed page (docs/09 route
    // focus handoff) — NOT back on the trigger, which would strand the user
    // at the top of the chrome after every palette jump.
    await waitFor(() =>
      expect(document.activeElement).toBe(document.getElementById("main-content")),
    );
  });

  it("a close that does NOT navigate still restores focus to its opener", async () => {
    renderShell();
    const field = await openPalette("command-palette-trigger-mobile");
    expect(document.activeElement).toBe(field);
    // Escape → the shell router closes the palette; nothing routed, so the
    // dialogA11y restore is the whole story (the handoff only runs on a real
    // pathname change).
    fireEvent.keyDown(field, { key: "Escape" });
    await waitFor(() => expect(palette()).toBeNull());
    expect(location()).toBe("/dashboard");
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByTestId("command-palette-trigger-mobile"),
      ),
    );
  });

  it("arrow keys move the active option and Enter runs it", async () => {
    renderShell();
    const field = await openPalette("command-palette-trigger-mobile");
    fireEvent.keyDown(field, { key: "ArrowDown" });
    fireEvent.keyDown(field, { key: "ArrowDown" });
    const options = screen.getAllByRole("option");
    // Registry order on an empty query: Transactions, Budgets, Reports, …
    expect(options[2].textContent).toContain("Reports");
    expect(options[2].getAttribute("aria-selected")).toBe("true");
    expect(options[0].getAttribute("aria-selected")).toBe("false");
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(location()).toBe("/reports"));
  });

  it("jumps straight to a report tab route", async () => {
    renderShell();
    const field = await openPalette("command-palette-trigger-mobile");
    fireEvent.change(field, { target: { value: "dividends" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(location()).toBe("/reports?tab=dividends"));
  });

  it("Take the tour fires the tour's own resurface event and navigates nowhere", async () => {
    renderShell();
    const started = vi.fn();
    window.addEventListener("fin_tracker:tour:start", started);
    const field = await openPalette("command-palette-trigger-mobile");
    fireEvent.change(field, { target: { value: "tour" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(started).toHaveBeenCalledTimes(1));
    expect(location()).toBe("/dashboard");
    expect(palette()).toBeNull();
    window.removeEventListener("fin_tracker:tour:start", started);
  });

  it("writes the honest no-results state", async () => {
    renderShell();
    const field = await openPalette("command-palette-trigger-mobile");
    fireEvent.change(field, { target: { value: "zzzznothing" } });
    expect(await screen.findByTestId("command-palette-empty")).toBeTruthy();
    expect(screen.getByTestId("command-palette-empty").textContent).toBe(
      "No match — Try Import or Help",
    );
  });

  it("announces loading and rejected search states politely without retry backoff", async () => {
    let rejectSearch!: (error: Error) => void;
    vi.mocked(searchGlobal).mockReturnValueOnce(
      new Promise((_, reject) => {
        rejectSearch = reject;
      }),
    );
    // Production query defaults to retries; the palette must still expose the
    // terminal error promptly and must remove the stale loading announcement.
    renderShell("/help", 3);
    const field = await openPalette("command-palette-trigger-mobile");
    fireEvent.change(field, { target: { value: "mockquery" } });

    const loading = await screen.findByTestId("global-search-loading");
    expect(loading.getAttribute("role")).toBe("status");
    expect(loading.getAttribute("aria-live")).toBe("polite");
    await act(async () => {
      rejectSearch(new Error("mock deterministic rejection"));
    });
    const error = await screen.findByTestId("global-search-error");
    expect(error.getAttribute("role")).toBe("status");
    expect(error.getAttribute("aria-live")).toBe("polite");
    expect(screen.queryByTestId("global-search-loading")).toBeNull();
    expect(searchGlobal).toHaveBeenCalledTimes(1);
  });

  it("Ctrl+K opens from dashboard focus and Escape restores the Search launcher", async () => {
    renderShell();
    const launcher = await screen.findByTestId("command-palette-trigger-mobile");
    // jsdom has no layout, so expose this rendered launcher as visible to the
    // same target-selection check used in the browser.
    vi.spyOn(launcher, "getClientRects").mockReturnValue([{} as DOMRect]);
    const main = document.getElementById("main-content");
    expect(main).not.toBeNull();
    main?.focus();
    expect(document.activeElement).toBe(main);

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const field = await screen.findByTestId("command-palette-input");
    expect(document.activeElement).toBe(field);
    // Escape goes through the shell's one window router.
    fireEvent.keyDown(field, { key: "Escape" });
    await waitFor(() => expect(palette()).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(launcher));
    expect(location()).toBe("/dashboard");
  });

  it("⌘K still toggles closed without adding a window listener", async () => {
    const addListener = vi.spyOn(window, "addEventListener");
    renderShell();
    await screen.findByTestId("command-palette-trigger-mobile");
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await screen.findByTestId("command-palette-input");
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await waitFor(() => expect(palette()).toBeNull());
    expect(addListener.mock.calls.filter(([type]) => type === "keydown")).toHaveLength(1);
  });
});

describe("drawer focus contract", () => {
  it("moves focus into the mobile drawer and restores the opener when it closes", async () => {
    renderShell();
    const toggle = await screen.findByTestId("nav-drawer-toggle");
    toggle.focus();
    fireEvent.click(toggle);

    const drawer = await screen.findByTestId("app-nav");
    await waitFor(() => expect(document.activeElement).toBe(drawer));
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(screen.getByTestId("nav-drawer-close"));
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("false"));
    await waitFor(() => expect(document.activeElement).toBe(toggle));
  });
});

describe("palette result semantics and keyboard safety", () => {
  it("exposes record kinds in each option's accessible name", async () => {
    vi.mocked(searchGlobal).mockResolvedValueOnce({
      transactions: [{ id: 71, normalized_payee: "Northstar Market", category_name: "Groceries", encrypted: false }],
      categories: [{ id: 72, name: "Northstar groceries" }],
      accounts: [{ id: 73, name: "Northstar chequing", type: "chequing" }],
      securities: [{ id: 74, symbol: "NSTR", name: "Northstar Inc.", exchange: "NASDAQ" }],
      tags: [{ id: 75, name: "Northstar" }],
      withheld: 0,
    });
    renderShell();
    const field = await openPalette("command-palette-trigger-mobile");
    fireEvent.change(field, { target: { value: "northstar" } });

    const results = within(await screen.findByTestId("command-palette-listbox"));
    await results.findByRole("option", { name: /Northstar Market.*Transaction/ });
    for (const [label, group] of [
      ["Northstar Market", "Transaction"],
      ["Northstar groceries", "Category"],
      ["Northstar chequing", "Account"],
      ["NSTR", "Security"],
      ["Northstar", "Tag"],
    ]) {
      expect(results.getByRole("option", { name: new RegExp(`${label}.*${group}`) })).toBeTruthy();
    }
  });

  it("keeps keyboard traversal limited to server-withheld-safe results while locked", async () => {
    lockVault();
    vi.mocked(searchGlobal).mockResolvedValueOnce({
      transactions: [],
      categories: [{ id: 82, name: "Northstar category" }],
      accounts: [],
      securities: [],
      tags: [],
      withheld: 1,
    });
    renderShell();
    const field = await openPalette("command-palette-trigger-mobile");
    fireEvent.change(field, { target: { value: "northstar" } });

    const results = await screen.findByTestId("command-palette-listbox");
    const categoryOption = await within(results).findByRole("option", { name: /Northstar category/ });
    expect(categoryOption.textContent).toContain("Category");
    expect(results.textContent).not.toContain("Sensitive encrypted payee");
    expect(searchGlobal).toHaveBeenCalledWith("northstar", undefined, true, 10);

    fireEvent.keyDown(field, { key: "ArrowDown" });
    const activeId = field.getAttribute("aria-activedescendant");
    expect(activeId).toBeTruthy();
    const activeOption = document.getElementById(activeId as string);
    expect(activeOption?.getAttribute("role")).toBe("option");
    expect(activeOption?.textContent).not.toContain("Sensitive encrypted payee");
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(location()).toBe("/transactions?category=82"));
  });
});

describe("Escape ordering against the nav drawer (docs/09 §Escape routing)", () => {
  it("press 1 closes only the palette; press 2 closes the drawer", async () => {
    renderShell();
    const toggle = await screen.findByTestId("nav-drawer-toggle");
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));

    const field = await openPalette("command-palette-trigger-mobile");
    // Both layers are up: the drawer behind, the palette on top.
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    fireEvent.keyDown(field, { key: "Escape" });
    await waitFor(() => expect(palette()).toBeNull());
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("false"));
  });

  it("registers no new keydown listener across palette open/close cycles", async () => {
    const add = vi.spyOn(window, "addEventListener");
    renderShell();
    const baseline = add.mock.calls.filter(([type]) => type === "keydown").length;
    for (let i = 0; i < 3; i += 1) {
      fireEvent.keyDown(window, { key: "k", metaKey: true });
      await waitFor(() => expect(palette()).not.toBeNull());
      fireEvent.keyDown(window, { key: "k", metaKey: true });
      await waitFor(() => expect(palette()).toBeNull());
    }
    // The palette cycle may not re-register anything — the shell's Escape
    // router and the ⌘K hook each signed up once at mount, and the M72 F1
    // race (a listener unhooked mid-dispatch) cannot re-form around a
    // fourth Escape surface.
    expect(add.mock.calls.filter(([type]) => type === "keydown")).toHaveLength(baseline);
    add.mockRestore();
  });
});

describe("triggers and console hygiene", () => {
  it("the mobile header magnifier exists at the 44px floor with dialog semantics", async () => {
    renderShell();
    const trigger = await screen.findByTestId("command-palette-trigger-mobile");
    expect(trigger.getAttribute("aria-haspopup")).toBe("dialog");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(trigger.getAttribute("aria-label")).toBe("Open command palette");
    // size-11 = 2.75rem = 44px — the standing touch floor (docs/09).
    expect(trigger.className).toContain("size-11");
  });

  it.each([360, 390])(
    "logged-in appbar at %dpx: fixed 44px mask chip cannot evict the 44px palette trigger and the cluster always fits",
    async (viewportWidth: number) => {
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: viewportWidth,
      });
      renderShell();
      const trigger = await screen.findByTestId("command-palette-trigger-mobile");
      const chip = screen.getByTestId("mask-chip");
      const appbarRow = trigger.closest("header")?.firstElementChild as HTMLElement;
      expect(appbarRow?.className).toContain(
        "grid-cols-[2.75rem_minmax(0,1fr)_auto]",
      );

      // jsdom has no layout engine, so the geometry below pins the
      // CASCADE-RESOLVED dimensions, not an anchored fake: every utility's px
      // meaning is declared once here, each number is read back from the
      // computed style, and the row math is the appbar grid's own template —
      // `2.75rem | minmax(0,1fr) | auto` — with the brand track held at its
      // minmax(0,…) 0 floor, the worst case the browser can reach at 360px.
      // QA-360-TOUCH-REG (prod, candidate 2df3629): the collapsed mask chip
      // kept `w-full` + `px-3` next to its `px-0` collapsed override, so its
      // percentage width resolved to the whole 110px cluster max-content and
      // evicted the trailing shrink-0 44px trigger to x=358..402 — ~2px on
      // screen, center tap impossible at BOTH widths. Under that code this
      // test fails exactly where it should: the chip's computed width reads
      // 100% and its padding 12px.
      const style = document.createElement("style");
      style.textContent = `
        .size-11 { width: 44px; height: 44px; }
        .w-11 { width: 44px; }
        .w-full { width: 100%; }
        .px-0 { padding-left: 0px; padding-right: 0px; }
        .px-3 { padding-left: 12px; padding-right: 12px; }
        .px-2 { padding-left: 8px; padding-right: 8px; }
      `;
      document.head.appendChild(style);
      try {
        // The collapsed chip must resolve to the FIXED 44px square, never a
        // percentage of the cluster's auto width.
        expect(Number.parseFloat(getComputedStyle(chip).width)).toBe(44);
        // The cascade may not hand the expanded chip's horizontal padding to
        // the collapsed control either (px-0 must not ride next to px-3).
        expect(Number.parseFloat(getComputedStyle(chip).paddingLeft)).toBe(0);
        expect(Number.parseFloat(getComputedStyle(chip).paddingRight)).toBe(0);
        // The trigger keeps the 44px floor from its own resolved utility.
        expect(getComputedStyle(trigger).width).toBe("44px");
        expect(getComputedStyle(trigger).height).toBe("44px");
        // …and the trigger is still the cluster's LAST child — the row
        // anchored to the right padding edge — with the chip directly before
        // it, so the arithmetic below speaks about the real structure.
        const cluster = trigger.parentElement as HTMLElement;
        expect(cluster.lastElementChild).toBe(trigger);
        expect(trigger.previousElementSibling).toBe(chip);

        // Row arithmetic — the auto (right) track's minimum occupancy with the
        // middle track squeezed to zero:
        //   8 (row px-2) + 44 (toggle) + 8 (row gap-2) + 0 (brand floor)
        // + 8 (row gap-2) + 44 (chip) + 6 (cluster gap-1.5) + 44 (trigger)
        // + 8 (row px-2) = 170.
        const rowPad = Number.parseFloat(getComputedStyle(appbarRow).paddingLeft);
        expect(rowPad).toBe(8);
        expect(appbarRow.className).toContain("gap-2");
        expect(cluster.className).toContain("gap-1.5");
        const fitsWithBrandAtZeroFloor =
          rowPad + 44 + 8 + 0 + 8 + 44 + 6 + 44 + rowPad <= viewportWidth;
        expect(fitsWithBrandAtZeroFloor).toBe(true);
        // An appbar row wider than the viewport guarantees SOME control sits
        // off-screen regardless of internal order — the regression's shape.
        expect(viewportWidth).toBeGreaterThanOrEqual(rowPad + 44 + 8 + 8 + 44 + 6 + 44 + rowPad);
      } finally {
        style.remove();
      }
    },
  );

  it.each([360, 390])(
    "logged-out appbar at %dpx: the same cluster contract holds without a user session",
    async (viewportWidth: number) => {
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: viewportWidth,
      });
      authState.user = null;
      try {
        renderShell();
        const trigger = await screen.findByTestId("command-palette-trigger-mobile");
        const chip = screen.getByTestId("mask-chip");
        const style = document.createElement("style");
        style.textContent = `
          .size-11 { width: 44px; height: 44px; }
          .w-11 { width: 44px; }
          .w-full { width: 100%; }
          .px-2 { padding-left: 8px; padding-right: 8px; }
        `;
        document.head.appendChild(style);
        try {
          // Same discriminating reads as the logged-in case: a fixed chip
          // width and no horizontal padding riding along.
          expect(Number.parseFloat(getComputedStyle(chip).width)).toBe(44);
          expect(Number.parseFloat(getComputedStyle(chip).paddingLeft)).toBe(0);
          expect(getComputedStyle(trigger).width).toBe("44px");
          expect(getComputedStyle(trigger).height).toBe("44px");
          const rowPad = Number.parseFloat(
            getComputedStyle(trigger.closest("header")?.firstElementChild as Element).paddingLeft,
          );
          expect(rowPad + 44 + 8 + 0 + 8 + 44 + 6 + 44 + rowPad).toBeLessThanOrEqual(
            viewportWidth,
          );
        } finally {
          style.remove();
        }
      } finally {
        authState.user = { id: "test-user", email: "test@example.com" };
      }
    },
  );

  it("the top bar shows its own palette trigger when labels are visible", async () => {
    // M185 (docs/47 §1.1): the rail's "Command palette" ROW became the
    // top bar's search trigger — same dialog semantics, same ⌘K shortcut.
    desktopViewport = true;
    renderShell();
    const trigger = await screen.findByTestId("command-palette-trigger");
    expect(trigger.getAttribute("aria-haspopup")).toBe("dialog");
    expect(trigger.textContent).toContain("Search");
    expect(trigger.textContent).toMatch(/⌘K|Ctrl\+K/);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("logs zero console errors across open / type / navigate / reopen", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const warns = vi.spyOn(console, "warn").mockImplementation(() => {});
    renderShell();
    for (const query of ["bud", "report", "zzz", ""]) {
      const field = await openPalette("command-palette-trigger-mobile");
      if (query !== "") fireEvent.change(field, { target: { value: query } });
      if (query === "bud") {
        fireEvent.keyDown(field, { key: "Enter" });
        await waitFor(() => expect(location()).toBe("/budgets"));
      } else {
        fireEvent.keyDown(field, { key: "Escape" });
        await waitFor(() => expect(palette()).toBeNull());
      }
    }
    expect(errors).not.toHaveBeenCalled();
    expect(warns).not.toHaveBeenCalled();
    errors.mockRestore();
    warns.mockRestore();
  });
});

describe("axe-core: zero violations on the open palette (docs/09 §Command palette)", () => {
  it("zero violations on the OPEN palette — no nested interactive inside role=option", async () => {
    // M95 axe fix: the option rows used to wrap their label in a real
    // <button>, which makes every `role="option"` an interactive control
    // holding an interactive descendant. Live axe on the production build
    // reported `nested-interactive` on all 8 default rows (impact=serious).
    // This test is the pin so the regression cannot come back.
    renderShell();
    await openPalette("command-palette-trigger-mobile");
    const options: axe.RunOptions = {
      resultTypes: ["violations"],
      // jsdom paints nothing AND loads no stylesheet, so the palette scrim is
      // not computed `position: fixed` there and axe's `region` rule reports
      // the overlay as loose at <body> — it does NOT fire on the real build
      // (channel A's live scan of the open palette reports zero `region`
      // nodes, which is the authority for landmark structure). Contrast is
      // disabled for the same reason as helpHint.test.ts.
      rules: {
        "color-contrast": { enabled: false },
        region: { enabled: false },
      },
    };
    const open = await axe.run(document.body, options);
    expect(open.violations.map((v) => `${v.id}(${v.impact})`)).toEqual([]);

    // And the structure the fix relies on is the one actually in the DOM.
    const listbox = screen.getByTestId("command-palette-listbox");
    const optionRows = within(listbox).getAllByRole("option");
    expect(optionRows.length).toBeGreaterThan(0);
    for (const row of optionRows) {
      expect(row.querySelector("button, a, input, select, textarea")).toBeNull();
    }
  });
});

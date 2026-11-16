// M100 (re-pointed by M185, docs/47 §1.1–1.2) — the accounts destination set
// across the LEDGER chrome: the five top tabs, the More panel's three groups,
// the account menu and the phone drawer, plus the /accounts index itself.
//
// The M100 bug this file was written to pin — clicking ACCOUNTS in the
// collapsed sidebar landed on Settings' Security card (spec-blessed
// NEVER-WORKED from M21, fixed by the real /accounts index) — is re-pinned on
// its SURVIVING surfaces: the More panel's "All accounts" row and the
// drawer's, both of which must land on the index, and every per-account row on
// /accounts/{id}. The rail-specific contracts (collapsed icon labels, the
// RailTip bubble, the collapse toggle) retired WITH the rail: the panel rows
// carry visible labels, so no aria-label mirror is needed, and the tooltip job
// moved to the panel's own rows (docs/47 §1.1).
//
// Harness note: this file drives the REAL `AppShell` inside a `MemoryRouter`
// with a location probe and NO child routes, so navigation is asserted from
// the router itself — no destination page module is imported.
import { existsSync } from "node:fs";
import path from "node:path";
import axe from "axe-core";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../src/components/layout/AppShell";
import {
  matchPaletteEntries,
  paletteEntries,
} from "../src/components/layout/CommandPalette";
import { Accounts } from "../src/pages/Accounts";
import { ThemeProvider } from "../src/theme/ThemeProvider";

const HELP_DIR = path.resolve(__dirname, "..", "public", "help");

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
}));

vi.mock("../src/api/accounts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/accounts")>();
  return { ...actual, listAccounts: mocks.listAccounts };
});

vi.mock("../src/api/notifications", () => ({
  INBOX_QUERY_KEY: ["notifications", "inbox"],
  getNotificationInbox: vi.fn().mockResolvedValue({ items: [], unread_count: 0 }),
  markNotificationRead: vi.fn().mockResolvedValue(undefined),
  markAllNotificationsRead: vi.fn().mockResolvedValue(undefined),
  unreadBadgeLabel: () => "",
}));

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({
    user: {
      id: "m100-user",
      email: "m100@example.com",
      privacy_mode: false,
      // On, so the Tools group is in scope for the audit below (docs/47 §1.1:
      // the rail's currency-tracker gate survives on the More/drawer group).
      currency_tracker_enabled: true,
      dashboard_preset: "overview",
    },
    logout: vi.fn(),
  }),
}));

vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

/** True while the emulated viewport is >= 768px (the shell's breakpoint). */
let desktopViewport = true;

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

const account = (over: Partial<Record<string, unknown>> = {}) => ({
  id: 1,
  institution_id: null,
  name: "Everyday chequing",
  type: "chequing",
  currency: "CAD",
  taxable: false,
  llm_autopilot: false,
  cash_balance: "1250.00",
  investment_value: "0.00",
  total_value: "1250.00",
  starting_balance: "0.00",
  name_cipher: null,
  ...over,
});

const page = (items: unknown[]) => ({
  items,
  total: items.length,
  limit: 200,
  offset: 0,
});

/** Renders the shell with a location probe and no child routes. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function renderShell(initialPath = "/dashboard") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <LocationProbe />
          <AppShell />
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const location = () => screen.getByTestId("location").textContent ?? "";

/** The five top tabs (docs/47 §1.1). */
const TAB_LINKS = [
  { label: "Dashboard", route: "/dashboard" },
  { label: "Transactions", route: "/transactions" },
  { label: "Imports", route: "/imports" },
  { label: "Investments", route: "/investments" },
  { label: "Reports", route: "/reports" },
] as const;

/** More > Primary (docs/47 §1.1). */
const PRIMARY_LINKS = [
  { label: "Forecast", route: "/forecast" },
  { label: "Assistant", route: "/assistant" },
  { label: "Retirement", route: "/retirement" },
  { label: "Calendar", route: "/calendar" },
  { label: "Taxes", route: "/taxes" },
  { label: "Tax Projection", route: "/tax-projection" },
  { label: "Shared household", route: "/shared" },
  { label: "Settings", route: "/settings" },
  { label: "Help", route: "/help" },
] as const;

/** More > Tools — gated on the currency tracker, exactly as the rail was. */
const TOOLS_LINKS = [
  { label: "Currencies", route: "/currencies" },
  { label: "Tags", route: "/tags" },
  { label: "Rules", route: "/rules" },
  { label: "Budgets", route: "/budgets" },
  { label: "Goals", route: "/goals" },
  { label: "Bills", route: "/bills" },
  { label: "Debts", route: "/debts" },
] as const;

/** Open the desktop More panel (docs/47 §1.1). */
async function openMore(): Promise<HTMLElement> {
  fireEvent.click(await screen.findByTestId("more-toggle"));
  return screen.findByTestId("more-panel");
}

beforeEach(() => {
  desktopViewport = true;
  window.localStorage.setItem("fin_tracker.tour.v1.m100-user", "completed");
  window.localStorage.removeItem("theme");
  mocks.listAccounts.mockResolvedValue(
    page([
      account(),
      account({ id: 2, name: "TFSA", type: "tfsa", total_value: "900.00" }),
      account({ id: 3, name: "Visa", type: "credit_card", total_value: "-250.00" }),
    ]),
  );
});

afterEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
});

// ── 1. The regression this card exists for, on its surviving surfaces ────────

describe("M100 regression: the accounts entry goes to the accounts page", () => {
  it("the More panel's All accounts row goes to /accounts (never /settings)", async () => {
    renderShell();
    const panel = await openMore();

    const all = within(panel).getByRole("link", { name: /All accounts/ });
    expect(all.getAttribute("href")).toBe("/accounts");
    // The bug: the collapsed control used to be /settings (M21 → M100).
    expect(all.getAttribute("href")).not.toBe("/settings");

    fireEvent.click(all);
    await waitFor(() => expect(location()).toBe("/accounts"));
  });

  it("account rows go to /accounts/:id and the active row announces itself", async () => {
    renderShell("/accounts/2");
    const panel = await openMore();

    // The row that owns the CURRENT route is the CURRENT one — a highlighted
    // row must name the surface (docs/09 naming), not only change colour.
    const tfsa = within(panel).getByRole("link", { name: /TFSA/ });
    expect(tfsa.getAttribute("href")).toBe("/accounts/2");
    await waitFor(() => expect(tfsa.getAttribute("aria-current")).toBe("page"));

    // …and clicking another row navigates to ITS detail page.
    const every = within(panel).getByRole("link", { name: /Everyday chequing/ });
    expect(every.getAttribute("href")).toBe("/accounts/1");
    fireEvent.click(every);
    await waitFor(() => expect(location()).toBe("/accounts/1"));

    // The route change closes the panel (docs/47 §1.1: a navigation drops
    // every shell surface) — re-open it and the active row has moved.
    const reopened = await openMore();
    const everyAgain = within(reopened).getByRole("link", { name: /Everyday chequing/ });
    await waitFor(() => expect(everyAgain.getAttribute("aria-current")).toBe("page"));
  });
});

// ── 2. The audit: every destination, named, both themes ──────────────────────

describe.each(["dark", "light"] as const)("M100 Ledger nav audit — theme %s", (theme) => {
  beforeEach(() => {
    window.localStorage.setItem("theme", theme);
  });

  {
    it(`${theme}: every destination is named and lands on its route`, async () => {
      renderShell();
      // Prove the theme really is the one under test, not a silently-wrong
      // default (the `dark` class is what every token resolves against).
      await waitFor(() =>
        expect(document.documentElement.classList.contains("dark")).toBe(theme === "dark"),
      );

      for (const { label, route } of TAB_LINKS) {
        const link = screen.getByRole("link", { name: label });
        expect(link.getAttribute("href"), `${label} href`).toBe(route);
        // The 44px floor + the focus ring are the standing contracts the rail
        // rows carried; the tab strip keeps them (jsdom cannot lay out, so the
        // class is the contract the CSS implements).
        expect(link.className, `${label} focus ring`).toContain("focus-visible:ring-2");
      }

      for (const { label, route } of [...PRIMARY_LINKS, ...TOOLS_LINKS]) {
        // Each click navigates away, which closes the panel (the shell drops
        // every surface on a route change) — so re-open and RE-QUERY it each
        // time rather than holding a detached node.
        const panel = await openMore();
        const link = within(panel).getByRole("link", { name: label });
        expect(link.getAttribute("href"), `${label} href`).toBe(route);
        expect(link.className, `${label} size floor`).toContain("min-h-11");
        expect(link.className, `${label} focus ring`).toContain("focus-visible:ring-2");
        fireEvent.click(link);
        await waitFor(() => expect(location(), `${label} → route`).toBe(route));
      }
    });
  }
});

// ── 3. The account menu (the rail foot's survivor) ───────────────────────────

describe("M100 account menu (docs/47 §1.1)", () => {
  it("carries the session controls the rail foot used to hold", async () => {
    renderShell();
    fireEvent.click(await screen.findByTestId("account-menu-toggle"));
    // Theme switch, the second mask row (docs/46 §D1: one store, so the two
    // surfaces cannot disagree) and Log out, all reachable by name.
    expect(screen.getByRole("button", { name: /Switch to (light|dark) theme/ })).toBeTruthy();
    expect(screen.getByTestId("mask-toggle-row")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Log out" })).toBeTruthy();
  });

  it("announces the signed-in account", async () => {
    renderShell();
    const toggle = await screen.findByTestId("account-menu-toggle");
    expect(toggle.getAttribute("aria-label")).toBe("Account menu: m100@example.com");
  });
});

// ── 4. The page itself ───────────────────────────────────────────────────────

function renderAccounts() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <Accounts />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("/accounts index (docs/14 C10)", () => {
  it("lists every account under its sidebar group with its balance, rows to /accounts/:id", async () => {
    renderAccounts();
    // The heading renders before the request resolves — wait for the DATA.
    expect(await screen.findByRole("heading", { name: "Cash" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Accounts" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Registered" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Credit" })).toBeTruthy();

    const chequing = screen.getByRole("link", { name: /Everyday chequing/ });
    expect(chequing.getAttribute("href")).toBe("/accounts/1");
    expect(chequing.textContent).toContain("$1,250.00");

    expect(screen.getByRole("link", { name: /TFSA/ }).getAttribute("href")).toBe(
      "/accounts/2",
    );
    expect(screen.getByRole("link", { name: /Visa/ }).getAttribute("href")).toBe(
      "/accounts/3",
    );
    // No cross-currency arithmetic anywhere on this page: three accounts, no
    // summed "total" figure that would have to pick an FX rate.
    expect(screen.queryByText(/Total/i)).toBeNull();
  });

  it("renders the three-part empty state whose preview capture exists on disk", async () => {
    mocks.listAccounts.mockResolvedValue(page([]));
    renderAccounts();

    const empty = await screen.findByTestId("empty-state");
    expect(empty).toBeTruthy();
    // (1) what lives here
    expect(screen.getByTestId("empty-state-title").textContent).toBe(
      "No accounts yet",
    );
    // (2) a REAL capture of the populated page
    const img = screen.getByTestId("empty-state-preview").querySelector("img");
    expect(img?.getAttribute("src")).toBe("/help/accounts.png");
    expect((img?.getAttribute("alt") ?? "").length).toBeGreaterThan(20);
    expect(existsSync(path.join(HELP_DIR, "accounts.png"))).toBe(true);
    // (3) the one action that fills it — the Settings accounts manager
    const action = screen.getByTestId("empty-state-action");
    expect(action.getAttribute("href")).toBe("/settings");
    expect(action.textContent).toContain("Add an account");
  });

  it("surfaces a failed load as the shared error panel", async () => {
    mocks.listAccounts.mockRejectedValue(new Error("boom"));
    renderAccounts();
    const alert = await screen.findByRole("alert");
    expect(alert).toBeTruthy();
    // The shared ErrorPanel copy — `apiErrorMessage` renders a plain Error as
    // the generic panel line rather than leaking the raw message.
    expect(alert.textContent).toContain("Something went wrong");
  });
});

// ── 5. Reachability: ⌘K and the phone drawer ─────────────────────────────────

describe("M100 reachability", () => {
  it("the command palette registers /accounts and navigates there", async () => {
    expect(
      paletteEntries().some((entry) => entry.to === "/accounts" && entry.id === "page-accounts"),
    ).toBe(true);
    expect(matchPaletteEntries(paletteEntries(), "acc")[0]?.to).toBe("/accounts");

    desktopViewport = false;
    renderShell();
    const trigger = await screen.findByTestId("command-palette-trigger-mobile");
    trigger.focus();
    fireEvent.click(trigger);
    await waitFor(() => expect(screen.queryByTestId("command-palette")).not.toBeNull());
    const field = screen.queryByTestId("command-palette-input") as HTMLInputElement;
    fireEvent.change(field, { target: { value: "acc" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(location()).toBe("/accounts"));
  });

  it("the phone drawer carries every destination and its Accounts entry navigates", async () => {
    desktopViewport = false;
    renderShell();

    const parked = screen.getByTestId("app-nav");
    // Inert while parked: out of the focus order and the a11y tree.
    expect((parked as unknown as { inert?: boolean }).inert).toBe(true);

    fireEvent.click(await screen.findByTestId("nav-drawer-toggle"));
    await waitFor(() =>
      expect((screen.getByTestId("app-nav") as unknown as { inert?: boolean }).inert).toBe(false),
    );

    const open = within(screen.getByTestId("app-nav"));
    for (const { label } of [...TAB_LINKS, ...PRIMARY_LINKS, ...TOOLS_LINKS]) {
      expect(open.getByRole("link", { name: label }), label).toBeTruthy();
    }
    const accounts = open.getByRole("link", { name: /All accounts/ });
    expect(accounts.getAttribute("href")).toBe("/accounts");
    expect(accounts.className).toContain("min-h-11");
    fireEvent.click(accounts);
    await waitFor(() => expect(location()).toBe("/accounts"));
  });
});

// ── 6. axe — the open More panel ─────────────────────────────────────────────

describe("M100 axe — the open More panel", () => {
  it("reports zero violations", async () => {
    const { container } = renderShell();
    await openMore();
    const results = await axe.run(container, {
      resultTypes: ["violations"],
      // jsdom paints nothing and loads no stylesheet, so contrast cannot be
      // computed there — that audit is the live browser scan (docs/47 §14).
      rules: {
        "color-contrast": { enabled: false },
      },
    });
    expect(
      results.violations.map((violation) => `${violation.id}(${violation.impact})`),
    ).toEqual([]);
  });
});

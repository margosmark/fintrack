// M185 accessibility hardening — the Ledger shell's keyboard and focus
// journey for `TopBar`, `MobileTabBar` and `MorePanel` (docs/47 §6 D5, docs/09
// "Keyboard contract"), card t_23b8d18e.
//
// Four areas, each measured rather than restated from prose:
//
//  1. LOGICAL FOCUS ORDER — brand → five primary tabs → `More` → right cluster
//     in DOM order (which IS the visual order: the bar lays out left to
//     right); the popover sits between its toggle and the right cluster; the
//     bottom tab bar comes after the routed page; the closed drawer
//     contributes no stops (it is `inert`, pinned in m72Mobile).
//  2. ESCAPE / FOCUS RETURN — a surface closes from anywhere in the document
//     and hands focus back to the control that opened it; no shell surface
//     registers a keydown listener of its own (docs/09 §Escape routing: one
//     listener, more consumers); an open popup that CLAIMS `role="menu"` must
//     really be one (axe's `aria-required-children`).
//  3. VISIBLE FOCUS — every focusable that suppresses the global baseline
//     outline replaces it with a full-strength ring (docs/09, M66), and no
//     surface reaches for a roving `tabindex`: every destination is a native
//     link/button and the active tab is `aria-current="page"`.
//  4. ROUTE-FOCUS HANDOFF — leaving the chrome through a tab, the More panel
//     or the bottom tab bar must park focus in the routed page, not on the
//     control just used; first paint and an open modal are both left alone.

import axe from "axe-core";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../src/components/layout/AppShell";
import { MorePanel } from "../src/components/shell/MorePanel";

const mocks = vi.hoisted(() => ({ listAccounts: vi.fn() }));

vi.mock("../src/api/accounts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/accounts")>();
  return { ...actual, listAccounts: mocks.listAccounts };
});
vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({
    user: {
      id: "user-1",
      email: "demo@fintracker.local",
      currency_tracker_enabled: true,
    },
    logout: vi.fn(),
  }),
}));
vi.mock("../src/theme/ThemeProvider", () => ({
  useTheme: () => ({ theme: "dark", toggleTheme: vi.fn() }),
}));
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

/** The shell's breakpoint: true = TopBar chrome, false = appbar + tab bar. */
let desktop = true;

Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: /min-width/.test(query) ? desktop : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

/** The same selector dialogA11y uses, minus the visibility filter (no layout). */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function stops(root: ParentNode): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
}

let goTo: (to: string) => void = () => {};

/** Exposes `useNavigate` so a test can drive a route change programmatically. */
function Capture() {
  goTo = useNavigate();
  return null;
}

function Stub({ id }: { id: string }) {
  return (
    <div data-testid={id}>
      <h1>{id}</h1>
      <a href="#anchor">in-page anchor</a>
    </div>
  );
}

function renderShell(initialEntry = "/dashboard") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Capture />
      <QueryClientProvider client={client}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/dashboard" element={<Stub id="stub-dashboard" />} />
            <Route path="/calendar" element={<Stub id="stub-calendar" />} />
            <Route path="/transactions" element={<Stub id="stub-transactions" />} />
            <Route path="/budgets" element={<Stub id="stub-budgets" />} />
            <Route path="*" element={<Stub id="stub-any" />} />
          </Route>
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const main = () => document.getElementById("main-content");

beforeEach(() => {
  desktop = true;
  mocks.listAccounts.mockResolvedValue({
    items: [],
    total: 0,
    limit: 200,
    offset: 0,
  });
  goTo = () => {};
});
afterEach(() => {
  vi.clearAllMocks();
});

// ── 1. logical focus order ───────────────────────────────────────────────────

describe("logical focus order — TopBar (docs/47 §1.1)", () => {
  it("walks brand → five primary tabs → More → right cluster in DOM order", async () => {
    renderShell();
    const topbar = await screen.findByTestId("app-topbar");
    const order = stops(topbar);

    const brand = screen.getByRole("link", { name: /fin_tracker — Dashboard/ });
    // Scoped to the Primary landmark: the brand links to /dashboard too.
    const tabs = within(screen.getByTestId("topbar-tabs")).getAllByRole("link");
    const more = screen.getByTestId("more-toggle");

    expect(tabs).toHaveLength(5);
    // The brand is the first stop, the tabs follow in their painted order, and
    // `More` is the last stop INSIDE the Primary landmark.
    expect(order.indexOf(brand)).toBe(0);
    tabs.forEach((tab, i) => expect(order.indexOf(tab)).toBe(i + 1));
    expect(order.indexOf(more)).toBe(6);

    // Everything in the right cluster comes after `More` — the popover opens
    // between the two, so Tab never walks into controls the panel covers.
    for (const id of [
      "command-palette-trigger",
      "mask-chip",
      "notifications-toggle",
      "account-menu-toggle",
    ]) {
      const el = screen.getByTestId(id);
      expect(order.indexOf(el), `${id} after More`).toBeGreaterThan(6);
    }
  });

  it("keeps the More popover between its toggle and the right cluster", async () => {
    renderShell();
    const topbar = await screen.findByTestId("app-topbar");
    fireEvent.click(await screen.findByTestId("more-toggle"));
    const panel = await screen.findByTestId("more-panel");

    const order = stops(topbar);
    const more = screen.getByTestId("more-toggle");
    const palette = screen.getByTestId("command-palette-trigger");
    // The panel is a child of the bar, so its links are focusable through the
    // bar: toggle → panel contents → right cluster, never the reverse.
    expect(order.indexOf(more)).toBeLessThan(order.indexOf(stops(panel)[0] as HTMLElement));
    expect(order.indexOf(stops(panel).at(-1) as HTMLElement)).toBeLessThan(
      order.indexOf(palette),
    );
  });

  it("puts the bottom tab bar after the routed page", async () => {
    desktop = false;
    renderShell();
    const bar = await screen.findByTestId("mobile-tab-bar");
    const mainEl = main() as HTMLElement;
    expect(mainEl).not.toBeNull();
    // documentPosition FOLLOWING = the bar is rendered after `main`, so Tab
    // walks the page before the fixed bar (visual order: bar is at the foot).
    expect(
      mainEl.compareDocumentPosition(bar) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});

// ── 2. Escape / focus return ─────────────────────────────────────────────────

describe("Escape and focus return (docs/47 §1.1, docs/09 §Escape routing)", () => {
  it("More: Escape from inside the panel closes it and returns focus to the toggle", async () => {
    renderShell();
    const toggle = await screen.findByTestId("more-toggle");
    // Chrome focuses a button on mousedown, so that is the element
    // dialogA11y must record as the opener — reproduce it here.
    act(() => toggle.focus());
    fireEvent.click(toggle);
    const panel = await screen.findByTestId("more-panel");
    await waitFor(() => expect(document.activeElement).toBe(panel));

    const calendar = within(panel).getByRole("link", { name: "Calendar" });
    act(() => calendar.focus());
    fireEvent.keyDown(calendar, { key: "Escape" });

    await waitFor(() => expect(screen.queryByTestId("more-panel")).toBeNull());
    expect(document.activeElement).toBe(toggle);
  });

  it("More: the panel registers NO keydown listener of its own", () => {
    const add = vi.spyOn(window, "addEventListener");
    render(
      <MemoryRouter>
        <MorePanel
          open
          onClose={() => {}}
          accounts={[]}
          toolsEnabled
          anchorRef={{ current: document.createElement("button") }}
        />
      </MemoryRouter>,
    );
    expect(
      add.mock.calls.filter(([type]) => type === "keydown"),
    ).toHaveLength(0);
    add.mockRestore();
  });

  it("the account menu: Escape returns focus to the avatar button", async () => {
    renderShell();
    const toggle = await screen.findByTestId("account-menu-toggle");
    act(() => toggle.focus());
    fireEvent.click(toggle);
    const panel = await screen.findByTestId("account-menu-panel");
    await waitFor(() => expect(document.activeElement).toBe(panel));

    const logout = within(panel).getByRole("button", { name: "Log out" });
    act(() => logout.focus());
    fireEvent.keyDown(logout, { key: "Escape" });

    await waitFor(() =>
      expect(screen.queryByTestId("account-menu-panel")).toBeNull(),
    );
    expect(document.activeElement).toBe(toggle);
  });

  it("the account menu is a popover, not a menu it never implements", async () => {
    renderShell();
    fireEvent.click(await screen.findByTestId("account-menu-toggle"));
    await screen.findByTestId("account-menu-panel");
    // A `role="menu"` promises menuitem children plus roving arrow-key focus;
    // this panel is an email line, a toggle and two buttons, so it is the same
    // disclosure the More button is.
    expect(
      screen
        .getByTestId("account-menu-panel")
        .getAttribute("role"),
    ).toBeNull();
    const trigger = screen.getByTestId("account-menu-toggle");
    expect(trigger.getAttribute("aria-haspopup")).toBeNull();
    expect(trigger.getAttribute("aria-controls")).toBe("account-menu-panel");
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
  });

  it("the notifications panel is the same disclosure — no promised menu", async () => {
    renderShell();
    const trigger = screen.getByTestId("notifications-toggle");
    expect(trigger.getAttribute("aria-haspopup")).toBeNull();
    expect(trigger.getAttribute("aria-controls")).toBe("notifications-panel");
    fireEvent.click(trigger);
    await screen.findByTestId("notifications-panel");
    expect(screen.getByTestId("notifications-panel").getAttribute("role")).toBeNull();
  });

  it("axe: the open account menu reports zero violations", async () => {
    const { container } = renderShell();
    fireEvent.click(await screen.findByTestId("account-menu-toggle"));
    await screen.findByTestId("account-menu-panel");
    const results = await axe.run(container, {
      resultTypes: ["violations"],
      // jsdom paints nothing: contrast is the live browser scan (docs/47 §14).
      rules: { "color-contrast": { enabled: false } },
    });
    expect(
      results.violations.map((v) => `${v.id}(${v.impact})`),
    ).toEqual([]);
  });

  it("negative control: putting role=menu back makes that axe run fail", async () => {
    const { container } = renderShell();
    fireEvent.click(await screen.findByTestId("account-menu-toggle"));
    const panel = await screen.findByTestId("account-menu-panel");
    // Restore the pre-hardening markup — an email line, a pressed toggle and
    // two plain buttons under `role="menu"`. Without this the green run above
    // could be an axe pass on a rule nobody ever saw fire.
    panel.setAttribute("role", "menu");
    const results = await axe.run(container, {
      resultTypes: ["violations"],
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations.map((v) => v.id)).toContain(
      "aria-required-children",
    );
  });
});

// ── 3. visible focus + no roving ─────────────────────────────────────────────

describe("visible focus and native tab stops (docs/09 Keyboard contract)", () => {
  // docs/09 Keyboard contract: two sanctioned indicators — a full-strength
  // ring, or the `.field-ring` input treatment (which opts out of the global
  // outline on purpose and paints border + halo instead).
  const RING = /focus-visible:ring-2 focus-visible:ring-(accent|danger)/;
  const indicated = (el: HTMLElement): boolean =>
    RING.test(el.className) || el.className.includes("field-ring");

  async function chrome(): Promise<HTMLElement> {
    renderShell();
    const topbar = await screen.findByTestId("app-topbar");
    return topbar;
  }

  it("every chrome control that drops the baseline outline replaces it with a ring", async () => {
    const topbar = await chrome();
    for (const el of stops(topbar)) {
      if (!el.className.includes("outline-none")) continue;
      expect(
        indicated(el),
        `${el.tagName}[${el.getAttribute("data-testid") ?? el.textContent}] suppresses the baseline without an indicator`,
      ).toBe(true);
    }
  });

  it("the More panel's own rows keep the ring", async () => {
    renderShell();
    fireEvent.click(await screen.findByTestId("more-toggle"));
    const panel = await screen.findByTestId("more-panel");
    for (const el of stops(panel)) {
      if (!el.className.includes("outline-none")) continue;
      expect(indicated(el), el.textContent ?? el.tagName).toBe(true);
    }
  });

  it("the bottom tab bar's five controls all keep the ring", async () => {
    desktop = false;
    renderShell();
    const bar = await screen.findByTestId("mobile-tab-bar");
    for (const el of stops(bar)) {
      expect(indicated(el), el.textContent ?? el.tagName).toBe(true);
    }
  });

  it("uses no roving tabindex — every stop is a native link or button", async () => {
    const topbar = await chrome();
    fireEvent.click(screen.getByTestId("more-toggle"));
    const panel = await screen.findByTestId("more-panel");
    for (const root of [topbar, panel]) {
      expect(root.querySelectorAll('[tabindex="0"]')).toHaveLength(0);
      for (const el of stops(root)) {
        expect(["A", "BUTTON", "INPUT"].includes(el.tagName), el.outerHTML.slice(0, 80)).toBe(true);
      }
    }
  });

  it("marks the active primary tab with aria-current=page, with no nested duplicate", async () => {
    await chrome();
    // Scoped to the Primary landmark: the brand points at /dashboard as well,
    // but the CURRENT-ROUTE marker belongs to the tab strip alone.
    const active = within(screen.getByTestId("topbar-tabs")).getAllByRole(
      "link",
      { current: "page" },
    );
    expect(active).toHaveLength(1);
    // No second `aria-current` nested inside the link — that would announce
    // "current page" twice for one control.
    expect(active[0]?.querySelectorAll("[aria-current]")).toHaveLength(0);
  });

  it("marks the active bottom tab with aria-current=page exactly once", async () => {
    desktop = false;
    renderShell();
    const bar = await screen.findByTestId("mobile-tab-bar");
    expect(within(bar).getAllByRole("link", { current: "page" })).toHaveLength(1);
  });
});

// ── 4. route-focus handoff ───────────────────────────────────────────────────

describe("route-focus handoff (docs/09 Keyboard contract)", () => {
  it("first paint leaves focus alone — the skip link stays the first stop", async () => {
    renderShell();
    await screen.findByTestId("app-topbar");
    expect(document.activeElement).not.toBe(main());
  });

  it("hands focus to the routed page after navigating from a primary tab", async () => {
    renderShell();
    const topbar = await screen.findByTestId("app-topbar");
    const tab = within(topbar).getByRole("link", { name: "Transactions" });
    act(() => tab.focus());
    fireEvent.click(tab);
    await waitFor(() => expect(screen.getByTestId("stub-transactions")).toBeTruthy());
    await waitFor(() => expect(document.activeElement).toBe(main()));
  });

  it("hands focus to the routed page from the More panel — not back to the toggle", async () => {
    renderShell();
    const toggle = await screen.findByTestId("more-toggle");
    act(() => toggle.focus());
    fireEvent.click(toggle);
    const panel = await screen.findByTestId("more-panel");
    const calendar = within(panel).getByRole("link", { name: "Calendar" });
    act(() => calendar.focus());
    fireEvent.click(calendar);
    await waitFor(() => expect(screen.getByTestId("stub-calendar")).toBeTruthy());
    // The panel's focus restore fires first (it hands focus back to the
    // toggle); the handoff must still win, or the user is parked in the chrome.
    await waitFor(() => expect(document.activeElement).toBe(main()));
  });

  it("hands focus to the routed page from the bottom tab bar", async () => {
    desktop = false;
    renderShell();
    const bar = await screen.findByTestId("mobile-tab-bar");
    const activity = within(bar).getByRole("link", { name: /Activity/ });
    act(() => activity.focus());
    fireEvent.click(activity);
    await waitFor(() => expect(screen.getByTestId("stub-transactions")).toBeTruthy());
    await waitFor(() => expect(document.activeElement).toBe(main()));
  });

  it("never steals focus from an open modal dialog", async () => {
    renderShell();
    await screen.findByTestId("app-topbar");
    // A modal surface (TransactionEditor / the palette) owns focus while it is
    // up; the handoff must leave it alone.
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.tabIndex = -1;
    document.body.append(dialog);
    act(() => dialog.focus());
    expect(document.activeElement).toBe(dialog);

    act(() => goTo("/calendar"));
    await waitFor(() => expect(screen.getByTestId("stub-calendar")).toBeTruthy());
    expect(document.activeElement).toBe(dialog);
    dialog.remove();
  });
});

// M68 — navigation & orphan-surface fixes (docs/09 "Routes", "App shell &
// auth flow"). Everything here runs against the REAL route table in App.tsx
// (`router` is exported), so the assertions are about the shipped wiring:
//
//  1. Calendar + Tax Projection now have sidebar rows (Calendar between
//     Reports and Taxes) and both navigate.
//  2. A deep link requested while logged out survives BOTH sign-in buttons —
//     the demo button used to hard-code `/dashboard`.
//  3. An unknown URL renders the branded NotFound with recovery affordances,
//     never React Router's "Unexpected Application Error!".
//  4. SecurityDetail's not-found panel links back to Investments.
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import App, { router } from "../src/App";
import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { SecurityDetail } from "../src/pages/SecurityDetail";
import type { User } from "../src/types";

const mocks = vi.hoisted(() => ({
  restoreSession: vi.fn(),
  login: vi.fn(),
  demoLogin: vi.fn(),
  listAccounts: vi.fn(),
  listCategories: vi.fn(),
  getNotificationInbox: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return {
    ...actual,
    restoreSession: mocks.restoreSession,
    login: mocks.login,
    demoLogin: mocks.demoLogin,
  };
});

vi.mock("../src/api/accounts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/accounts")>();
  return { ...actual, listAccounts: mocks.listAccounts };
});

vi.mock("../src/api/categories", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/categories")>();
  return { ...actual, listCategories: mocks.listCategories };
});

vi.mock("../src/api/notifications", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/notifications")>();
  return { ...actual, getNotificationInbox: mocks.getNotificationInbox };
});

// Destinations are stubbed so a routing assertion can never be masked by the
// destination page's own data fetching. NotFound is deliberately NOT stubbed —
// its rendering is exactly what is under test.
vi.mock("../src/pages/Dashboard", () => ({
  Dashboard: () => <div data-testid="stub-dashboard" />,
}));
vi.mock("../src/pages/Calendar", () => ({
  Calendar: () => <div data-testid="stub-calendar" />,
}));
vi.mock("../src/pages/TaxProjection", () => ({
  TaxProjection: () => <div data-testid="stub-tax-projection" />,
}));
vi.mock("../src/pages/Rules", () => ({
  Rules: () => <div data-testid="stub-rules" />,
}));

const USER: User = {
  id: "user-1",
  email: "demo@fintracker.local",
  privacy_mode: false,
  currency_tracker_enabled: false,
  llm_provider: "openai",
  llm_base_url: null,
  llm_model: "gpt-4o-mini",
  encryption_required: false,
  dashboard_preset: "overview",
};

const emptyPage = { items: [], total: 0, limit: 200, offset: 0 };

/** jsdom has no matchMedia; ThemeProvider + AppShell's breakpoint both need it. */
function installMatchMedia(): void {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: (query: string) => {
      const min = /min-width:\s*(\d+)px/.exec(query);
      const matches = min !== null ? window.innerWidth >= Number(min[1]) : false;
      return {
        matches,
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

function renderApp() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnMount: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <App />
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

async function goTo(path: string): Promise<void> {
  await act(async () => {
    await router.navigate(path, { replace: true });
  });
}

const currentPath = () => window.location.pathname;

beforeEach(async () => {
  mocks.restoreSession.mockResolvedValue({
    access_token: "token",
    token_type: "bearer",
    user: USER,
  });
  mocks.login.mockResolvedValue({ access_token: "token", token_type: "bearer", user: USER });
  mocks.demoLogin.mockResolvedValue({ access_token: "token", token_type: "bearer", user: USER });
  mocks.listAccounts.mockResolvedValue(emptyPage);
  mocks.listCategories.mockResolvedValue(emptyPage);
  mocks.getNotificationInbox.mockResolvedValue({ items: [], unread_count: 0 });
  await goTo("/dashboard");
});

describe("M185 nav rows — the five tabs, then the More panel", () => {
  it("the top bar carries exactly the five primary tabs in order", async () => {
    renderApp();
    await screen.findByRole("link", { name: "Dashboard" });

    const tabs = document.querySelector('[data-testid="topbar-tabs"]');
    expect(tabs).not.toBeNull();
    const hrefs = Array.from(
      (tabs as HTMLElement).querySelectorAll('a[href^="/"]'),
      (a) => a.getAttribute("href"),
    ).filter((href) => !href?.includes("#"));
    expect(hrefs).toEqual([
      "/dashboard",
      "/transactions",
      "/imports",
      "/investments",
      "/reports",
      // The More button is a <button>, so it is not in this <a> list.
    ]);
  });

  it("the More panel carries the remaining destinations and the accounts index", async () => {
    renderApp();
    fireEvent.click(await screen.findByTestId("more-toggle"));
    const panel = await screen.findByTestId("more-panel");
    const hrefs = Array.from(panel.querySelectorAll("a"), (a) => a.getAttribute("href"));
    // M109 (docs/45) Forecast right after Reports' group, M188 (docs/57)
    // Assistant and Retirement right after Forecast, M68 Calendar between
    // Reports and Taxes, M90 (docs/43 §13) Shared household, M100's accounts
    // index — the rail's full set, one click away behind More.
    expect(hrefs).toEqual([
      "/forecast",
      "/assistant",
      "/retirement",
      "/calendar",
      "/taxes",
      "/tax-projection",
      "/shared",
      "/settings",
      "/help",
      "/accounts",
    ]);
  });

  it("navigates to /calendar and /tax-projection from the More panel", async () => {
    renderApp();
    fireEvent.click(await screen.findByTestId("more-toggle"));

    fireEvent.click(await screen.findByRole("link", { name: "Calendar" }));
    await waitFor(() => expect(currentPath()).toBe("/calendar"));
    expect(await screen.findByTestId("stub-calendar")).toBeTruthy();

    fireEvent.click(await screen.findByTestId("more-toggle"));
    fireEvent.click(await screen.findByRole("link", { name: "Tax Projection" }));
    await waitFor(() => expect(currentPath()).toBe("/tax-projection"));
    expect(await screen.findByTestId("stub-tax-projection")).toBeTruthy();
  });
});

describe("M68 post-auth deep-link restore", () => {
  it("Explore the demo resumes the originally requested path", async () => {
    mocks.restoreSession.mockResolvedValue(null);
    await goTo("/rules");

    renderApp();

    const demo = await screen.findByRole("button", { name: /Explore the demo/ });
    // The guard bounced the logged-out deep link to /login first.
    expect(await screen.findByRole("heading", { name: "Welcome back" })).toBeTruthy();

    fireEvent.click(demo);

    await waitFor(() => expect(currentPath()).toBe("/rules"));
    expect(await screen.findByTestId("stub-rules")).toBeTruthy();
  });

  it("the password form resumes the same requested path", async () => {
    mocks.restoreSession.mockResolvedValue(null);
    await goTo("/reports");

    renderApp();

    fireEvent.change(await screen.findByPlaceholderText("Email"), {
      target: { value: "demo@fintracker.local" },
    });
    fireEvent.change(screen.getByPlaceholderText("Password"), {
      target: { value: "demo-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Log in$/ }));

    await waitFor(() => expect(currentPath()).toBe("/reports"));
  });

  it("falls back to /dashboard when there is no requested path", async () => {
    mocks.restoreSession.mockResolvedValue(null);
    await goTo("/login");

    renderApp();

    fireEvent.click(await screen.findByRole("button", { name: /Explore the demo/ }));

    await waitFor(() => expect(currentPath()).toBe("/dashboard"));
    expect(await screen.findByTestId("stub-dashboard")).toBeTruthy();
  });
});

describe("M68 branded 404", () => {
  it("renders the NotFound page instead of React Router's error boundary", async () => {
    await goTo("/no/such/page");

    renderApp();

    const notFound = await screen.findByTestId("not-found");
    expect(notFound).toBeTruthy();
    expect(document.body.textContent).not.toContain("Unexpected Application Error");
    expect(document.body.textContent).toContain("Page not found");
    expect(screen.getByRole("button", { name: "Go to Dashboard" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Back" })).toBeTruthy();
  });

  it("Go to Dashboard leaves the bad URL", async () => {
    await goTo("/definitely/not/a/route");

    renderApp();

    fireEvent.click(await screen.findByRole("button", { name: "Go to Dashboard" }));
    await waitFor(() => expect(currentPath()).toBe("/dashboard"));
    expect(await screen.findByTestId("stub-dashboard")).toBeTruthy();
  });

  it("an unauthenticated visitor is bounced to /login before any 404 copy", async () => {
    mocks.restoreSession.mockResolvedValue(null);
    await goTo("/no/such/page");

    renderApp();

    expect(await screen.findByRole("heading", { name: "Welcome back" })).toBeTruthy();
    expect(screen.queryByTestId("not-found")).toBeNull();
    expect(document.body.textContent).not.toContain("Unexpected Application Error");
  });
});

describe("M68 SecurityDetail recovery", () => {
  it("offers Back to Investments on the not-found panel", async () => {
    render(
      <MemoryRouter initialEntries={["/securities/not-a-number"]}>
        <QueryClientProvider
          client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
        >
          <ThemeProvider>
            <ToastProvider>
              <AuthProvider>
                <Routes>
                  <Route path="/securities/:securityId" element={<SecurityDetail />} />
                  <Route path="/investments" element={<div>investments page</div>} />
                </Routes>
              </AuthProvider>
            </ToastProvider>
          </ThemeProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );

    const back = await screen.findByRole("link", { name: "Back to Investments" });
    expect(back.getAttribute("href")).toBe("/investments");
    expect(screen.getByRole("alert")).toBeTruthy();
  });
});

// BUG (S2b review follow-up): the bell panel is absolute z-50 INSIDE the
// sticky backdrop-blur nav — that filter opens a stacking context, so the
// panel's z-50 was trapped and dashboard .glass cards (later in tree order)
// painted over it, occluding 54% of the panel / 3 of 5 buttons on the
// collapsed rail. The nav itself carries z-40: high enough to escape the
// card layer, but NOT z-50 — nav and MobileNav (z-40) then tie and tree
// order decides, with MobileNav rendered after the nav, so the bottom bar
// stays above the rail. Pin all three tokens so none can drift silently.
describe("AppShell stacking contract (chrome dropdowns vs content)", () => {
  it("the chrome block is z-40 and the bell panel is z-50 within it", async () => {
    // M185: the rail is gone; TopBar + TruthBar are ONE sticky z-40 block, and
    // the bell panel lives inside that block's stacking context (the M68 S2b
    // lesson — a panel inside a filtered container is trapped by it).
    renderApp();
    await screen.findByRole("link", { name: "Dashboard" });

    const topbar = screen.getByTestId("app-topbar");
    expect(topbar.className).toContain("z-40");

    const bell = await screen.findByRole("button", { name: /Notifications/ });
    fireEvent.click(bell);
    await waitFor(() => expect(bell.getAttribute("aria-expanded")).toBe("true"));
    const panel = topbar.querySelector("div.absolute.z-50") as HTMLElement | null;
    expect(panel).not.toBeNull();
  });

  it("the mobile tab bar renders after the chrome header, at z-40", async () => {
    // Tie-break guarantee on the phone: at equal z-index the bottom bar must
    // paint above the fixed header, which is tree order with the later node.
    // The shell's breakpoint reads matchMedia (wired to innerWidth here), so
    // the emulated viewport is pinned to a phone width for this test.
    cleanup();
    await act(async () => {
      await router.navigate("/dashboard", { replace: true });
    });
    Object.defineProperty(window, "innerWidth", { value: 390, configurable: true });
    try {
      renderApp();
      // The session probe gates the shell, so the header only exists once the
      // chrome has mounted — wait for the chrome, then read it.
      await screen.findByTestId("mobile-tab-bar");
      const header = document.querySelector("header.fixed");
      expect(header).not.toBeNull();
      const tabBar = document.querySelector(".mobile-bottom-nav")!;
      expect(tabBar.className).toContain("z-40");
      expect(
        header!.compareDocumentPosition(tabBar) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    } finally {
      Object.defineProperty(window, "innerWidth", { value: 1024, configurable: true });
    }
  });
});

describe("M97 coach marks survive every dashboard preset", () => {
  it("every Tour mark resolves under all three presets (marks target SURFACES)", async () => {
    // The marks come from Tour.tsx itself, so a mark added later is covered
    // without editing this test. Each is a `target` with an optional
    // `fallback` — the same pair the component resolves ([target, fallback],
    // first hit wins), so this asserts "at least one of the pair is
    // present", which is what "the mark stays resolvable" means.
    // They are all `[data-tour=...]` hooks rendered by AppShell — never a
    // dashboard card or a position in it — which is exactly why reordering
    // the dashboard (docs/09 §Dashboard layout presets) cannot strand one;
    // the card order itself is pinned by tests/m97DashboardPresets.test.tsx.
    const tourSource = readFileSync("src/components/tour/Tour.tsx", "utf8");
    const marks = [
      ...tourSource.matchAll(/target:\s*'([^']+)'(?:\s*,\s*fallback:\s*'([^']+)')?/g),
    ].map((m) => ({ target: m[1], fallback: m[2] }));
    expect(marks.length).toBeGreaterThanOrEqual(4);
    for (const mark of marks) expect(mark.target).toMatch(/^\[data-tour="[^"]+"\]$/);

    for (const dashboard_preset of ["overview", "import_first", "budget_first"] as const) {
      mocks.restoreSession.mockResolvedValue({
        access_token: "token",
        token_type: "bearer",
        user: { ...USER, dashboard_preset },
      });
      renderApp();
      await screen.findByRole("link", { name: "Dashboard" });
      const unresolved = marks
        .filter(
          (mark) =>
            document.querySelector(mark.target) === null &&
            (mark.fallback === undefined || document.querySelector(mark.fallback) === null),
        )
        .map((mark) => mark.target);
      expect(unresolved, `${dashboard_preset}: unresolved tour marks`).toEqual([]);
      cleanup();
    }
  });
});

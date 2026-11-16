// M139 — the mask CHIP, the ⌘⇧H shortcut, and the propagation guarantee
// (docs/09 § Display masking).
//
// The load-bearing claim of this feature is that ONE `useMasked()` call in
// AppShell re-renders everything under `<Outlet/>`, because this codebase has
// no React.money and no useMemo around money formatting. React Router caches
// route ELEMENTS, so that propagation is not obvious — the last describe below
// pins the format-layer half of the contract directly, and the live probe
// (frontend/scripts/a11y/m139_mask_probe.py) pins the routed half against the
// deployed build. House matcher style: `.getAttribute(...)`, no jest-dom.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../src/components/layout/AppShell";
import { ToastProvider } from "../src/components/ui/Toast";
import { formatCad } from "../src/lib/format";
import { MASK_SESSION_KEY, MASK_TEXT, initMaskForSession } from "../src/lib/mask";
import { ThemeProvider } from "../src/theme/ThemeProvider";

const mocks = vi.hoisted(() => ({ listAccounts: vi.fn() }));

// jsdom has no matchMedia; ThemeProvider (prefers-color-scheme), the shell's
// 768px breakpoint and framer-motion's reduced-motion check all ask for it.
// The stub is QUERY-AWARE on purpose: `useMinWidth(768)` reads
// `(min-width: 768px)`, and reporting `false` there drops the shell into its
// phone-drawer layout, where the chip is icon-only and these tests would be
// asserting on text that is never rendered. So: any min-width query = true
// (desktop rail, expanded 224px); everything else (prefers-color-scheme,
// prefers-reduced-motion) = false (dark theme, motion on).
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: /min-width/.test(query),
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

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
    user: { id: "1", email: "mark@example.com", currency_tracker_enabled: false },
    logout: vi.fn(),
  }),
  isReadOnlySession: () => false,
}));

/** A stand-in page that renders one real amount through the real formatter. */
function MoneyPage() {
  return <span data-testid="net-worth">{formatCad("151800.5")}</span>;
}

function renderShell() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={["/dashboard"]}>
            <AppShell />
          </MemoryRouter>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

const pressed = (element: HTMLElement): string | null =>
  element.getAttribute("aria-pressed");

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
  mocks.listAccounts.mockResolvedValue({ items: [], total: 0 });
});

afterEach(() => {
  sessionStorage.clear();
  initMaskForSession();
  vi.clearAllMocks();
});

describe("mask chip", () => {
  it("renders in the shell and flips the mask on click", async () => {
    renderShell();
    const chip = await screen.findByTestId("mask-chip");
    expect(pressed(chip)).toBe("false");
    fireEvent.click(chip);
    await waitFor(() => expect(pressed(chip)).toBe("true"));
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBe("1");
  });

  it("offers the switch again in the account-menu row, in sync with the chip", async () => {
    // M185 (docs/47 §1.1): the rail foot is gone — the second mask row lives
    // in the account menu, one store, so the two surfaces cannot disagree.
    renderShell();
    fireEvent.click(await screen.findByTestId("account-menu-toggle"));
    const row = await screen.findByTestId("mask-toggle-row");
    const chip = screen.getByTestId("mask-chip");
    expect(pressed(row)).toBe("false");
    fireEvent.click(row);
    await waitFor(() => expect(pressed(chip)).toBe("true"));
    expect(pressed(row)).toBe("true");
  });

  it("announces each flip through the aria-live region", async () => {
    renderShell();
    const live = await screen.findByTestId("mask-live-region");
    expect(live.getAttribute("aria-live")).toBe("polite");
    expect(live.textContent).toContain("Dollar figures are shown");
    fireEvent.click(screen.getByTestId("mask-chip"));
    await waitFor(() =>
      expect(live.textContent).toContain("Dollar figures are hidden"),
    );
  });

  it("differs by MORE than colour — icon and text both change", async () => {
    renderShell();
    const chip = await screen.findByTestId("mask-chip");
    // Unmasked reads "Unmasked"; masked reads "Masked". Paired with the eye /
    // eye-off glyphs in the component, the state never rides on colour alone.
    expect(chip.textContent).toContain("Unmasked");
    fireEvent.click(chip);
    await waitFor(() => expect(chip.textContent).toContain("Masked"));
  });
});

describe("⌘⇧H", () => {
  it("toggles the mask from the shell's single keydown listener", async () => {
    renderShell();
    const chip = await screen.findByTestId("mask-chip");
    expect(pressed(chip)).toBe("false");

    fireEvent.keyDown(window, { key: "H", metaKey: true, shiftKey: true });
    await waitFor(() => expect(pressed(chip)).toBe("true"));

    fireEvent.keyDown(window, { key: "H", metaKey: true, shiftKey: true });
    await waitFor(() => expect(pressed(chip)).toBe("false"));
  });

  it("accepts lowercase h and the Ctrl modifier too", async () => {
    renderShell();
    const chip = await screen.findByTestId("mask-chip");
    fireEvent.keyDown(window, { key: "h", ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(pressed(chip)).toBe("true"));
  });

  it("does NOT fire on plain ⌘H or a bare H", async () => {
    renderShell();
    const chip = await screen.findByTestId("mask-chip");
    fireEvent.keyDown(window, { key: "H", metaKey: true });
    fireEvent.keyDown(window, { key: "h" });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(pressed(chip)).toBe("false");
  });

  it("does not steal ⌘K from the command palette", async () => {
    renderShell();
    await screen.findByTestId("mask-chip");
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(pressed(screen.getByTestId("mask-chip"))).toBe("false");
  });
});

describe("the format layer flips with the shell", () => {
  it("changes what formatCad returns once the chip has been pressed", async () => {
    renderShell();
    await screen.findByTestId("mask-chip");
    expect(formatCad("151800.5")).toBe("$151,800.50");
    fireEvent.click(screen.getByTestId("mask-chip"));
    await waitFor(() =>
      expect(pressed(screen.getByTestId("mask-chip"))).toBe("true"),
    );
    expect(formatCad("151800.5")).toBe(MASK_TEXT);
  });
});

describe("a money component under the ambient mask", () => {
  it("prints bullets when the session boots masked, dollars when not", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const tree = (client: QueryClient) => (
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <MoneyPage />
        </ThemeProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(tree(queryClient));
    expect(screen.getByTestId("net-worth").textContent).toBe("$151,800.50");

    initMaskForSession({ demoDefault: true });
    rerender(tree(queryClient));
    expect(screen.getByTestId("net-worth").textContent).toBe(MASK_TEXT);
  });
});

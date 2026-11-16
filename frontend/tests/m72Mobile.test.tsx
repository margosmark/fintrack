// M72 — mobile-grade experience contracts (docs/09 §Mobile navigation,
// §Mobile & PWA).
//
// Three acceptance items live here:
//   1. the nav drawer opens and closes (toggle / close button / scrim / Escape),
//   2. `TableScroller` is engaged in a 390px-wide viewport (the M64 pattern the
//      card requires for Transactions at 390 — jsdom has no layout engine, so
//      the box is measured exactly the way the real one is: scrollWidth vs
//      clientWidth of the scroll box; the page-level proof that no horizontal
//      PAGE scroll exists is browser evidence recorded in docs/09),
//   3. the touch-target floor and the safe-area padding are actually in the
//      shipped source (CSS media query + env() insets).
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import path from "node:path";
import { useState } from "react";
import { flushSync } from "react-dom";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "../src/components/layout/AppShell";
import { TableScroller } from "../src/components/ui/TableScroller";

vi.mock("../src/api/accounts", () => ({
  listAccounts: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 }),
}));

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({
    user: { id: "test-user", email: "test@example.com" },
    logout: vi.fn(),
  }),
}));

vi.mock("../src/theme/ThemeProvider", () => ({
  useTheme: () => ({ theme: "dark", toggleTheme: vi.fn() }),
}));

vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

/** True while the emulated viewport is >= 768px (the shell's breakpoint). */
let desktopViewport = false;

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

function renderShell() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <QueryClientProvider client={client}>
        <AppShell />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const nav = () => document.getElementById("app-nav") as HTMLElement | null;
const inert = () => (nav() as unknown as { inert?: boolean } | null)?.inert;

/**
 * Same tree as `renderShell`, wrapped in a host component the test can force
 * to re-render — that is how the QA F1 race is reproduced deterministically
 * (a commit landing in the middle of a keydown dispatch).
 */
function renderShellWithHost() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  let bump: (() => void) | null = null;
  function Host() {
    const [, setTick] = useState(0);
    bump = () => setTick((tick) => tick + 1);
    return (
      <MemoryRouter initialEntries={["/dashboard"]}>
        <QueryClientProvider client={client}>
          <AppShell />
        </QueryClientProvider>
      </MemoryRouter>
    );
  }
  const utils = render(<Host />);
  return { ...utils, forceRender: () => bump?.() };
}

beforeEach(() => {
  desktopViewport = false;
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("M72 mobile navigation drawer", () => {
  it("starts closed: toggle collapsed, drawer inert", async () => {
    renderShell();
    const toggle = await screen.findByTestId("nav-drawer-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.getAttribute("aria-controls")).toBe("app-nav");
    expect(nav()).not.toBeNull();
    expect(inert()).toBe(true);
    expect(screen.queryByTestId("nav-scrim")).toBeNull();
  });

  it("opens from the toggle and closes again from the close button", async () => {
    renderShell();
    const toggle = await screen.findByTestId("nav-drawer-toggle");
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));
    expect(inert()).toBe(false);
    expect(screen.getByTestId("nav-scrim")).toBeTruthy();

    fireEvent.click(screen.getByTestId("nav-drawer-close"));
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("false"));
    expect(inert()).toBe(true);
    expect(screen.queryByTestId("nav-scrim")).toBeNull();
  });

  it("closes on Escape and on a scrim click", async () => {
    renderShell();
    const toggle = await screen.findByTestId("nav-drawer-toggle");

    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("false"));

    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));
    fireEvent.click(screen.getByTestId("nav-scrim"));
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("false"));
    expect(inert()).toBe(true);
  });

  // QA F1 regression — Escape must close the drawer on the INPUT path, in a
  // fresh session. `fireEvent.keyDown(window, …)` above is the synthetic
  // control that always passed (it starts at `window`, so it never reaches
  // React's root listener and can't reproduce the captured race); this test
  // starts the event at the focused element so it bubbles through the document
  // to `window`, the same DOM path a physical key / CDP trusted key takes.
  it("closes on a bubbling Escape keydown from the focused element", async () => {
    renderShell();
    const toggle = await screen.findByTestId("nav-drawer-toggle");

    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));

    toggle.focus();
    act(() => {
      (document.activeElement ?? document.body).dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
      );
    });
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("false"));
    expect(inert()).toBe(true);
  });

  // QA F1 root cause — the drawer's listener used to re-register on EVERY
  // render, so a render landing inside the same keydown dispatch (the search
  // handler's own setState did exactly that in production) deleted it before
  // the dispatcher reached it: Escape arrived, `hits: 0`, drawer stayed open.
  //
  // jsdom does not reproduce that interleaving on its own (React schedules the
  // search setState instead of flushing it inside the dispatch), so the render
  // is forced here — and the interposer is registered BEFORE the shell mounts,
  // putting it FIRST in the window dispatch order, ahead of the shell's own
  // listener: the exact slot the production race occupied. Whatever the
  // interposer commits must not be able to unhook the handler about to run.
  it("closes even when a render lands inside the same keydown dispatch", async () => {
    let force: (() => void) | null = null;
    const interpose = () => {
      if (force) flushSync(force);
    };
    window.addEventListener("keydown", interpose);
    try {
      const { forceRender } = renderShellWithHost();
      force = forceRender;
      const toggle = await screen.findByTestId("nav-drawer-toggle");

      fireEvent.click(toggle);
      await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));

      toggle.focus();
      act(() => {
        (document.activeElement ?? document.body).dispatchEvent(
          new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
        );
      });
      await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("false"));
    } finally {
      window.removeEventListener("keydown", interpose);
      force = null;
    }
  });

  // Unified search lives in the command palette, launched from the mobile
  // app bar; the navigation drawer must not grow a duplicate search surface.
  it("keeps one Search launcher in the mobile app bar and none in the drawer", async () => {
    renderShell();
    const toggle = await screen.findByTestId("nav-drawer-toggle");
    const header = screen.getByRole("banner");
    const launcher = within(header).getByTestId("command-palette-trigger-mobile");
    expect(within(header).getAllByRole("button", { name: /search|command palette/i })).toHaveLength(1);

    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));
    const drawer = screen.getByTestId("app-nav");
    expect(document.body.contains(launcher)).toBe(true);
    expect(within(drawer).queryByRole("textbox", { name: /search/i })).toBeNull();
    expect(within(drawer).queryByRole("button", { name: /search|command palette/i })).toBeNull();
  });

  // The class of bug, pinned structurally: the shell registers its Escape
  // handler ONCE and never tears it down while mounted, so no render — at any
  // time — can remove it. The old code registered a fresh listener per render.
  it("registers the window keydown listener exactly once while mounted", async () => {
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");

    renderShell();
    const toggle = await screen.findByTestId("nav-drawer-toggle");
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));
    // Re-render a few more times (route-independent state churn).
    fireEvent.click(screen.getByTestId("nav-drawer-close"));
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-expanded")).toBe("true"));

    const added = add.mock.calls.filter(([type]) => type === "keydown");
    const removed = remove.mock.calls.filter(([type]) => type === "keydown");
    expect(added).toHaveLength(1);
    expect(removed).toHaveLength(0);

    add.mockRestore();
    remove.mockRestore();
  });

  it("drops the drawer at md+ in favour of the TopBar chrome", async () => {
    // M185 (docs/47 §1.1): the rail is retired — desktop reaches every
    // destination through the TopBar's tabs + More panel, and the drawer is
    // not rendered at all at md+ (no parked copy of it in the DOM).
    desktopViewport = true;
    renderShell();
    await screen.findByTestId("app-topbar");
    expect(screen.queryByTestId("nav-drawer-toggle")).toBeNull();
    expect(screen.queryByTestId("nav-scrim")).toBeNull();
    expect(document.getElementById("app-nav")).toBeNull();
    // M68 stacking contract, re-pointed: the chrome block keeps its z-40
    // token so page cards can never outrank it.
    const topbar = screen.getByTestId("app-topbar");
    expect(topbar.className).toContain("z-40");
  });
});

describe("M72 TableScroller at 390px", () => {
  function stub(el: HTMLElement, scrollWidth: number, clientWidth: number) {
    Object.defineProperty(el, "scrollWidth", { value: scrollWidth, configurable: true });
    Object.defineProperty(el, "clientWidth", { value: clientWidth, configurable: true });
  }

  it("engages the scroll region + hint when the table overflows a 390px box", async () => {
    // 390px viewport, 20px page padding on each side -> a 350px content box,
    // which is what the transactions table gets once the rail is off-canvas.
    render(
      <TableScroller label="Transactions table">
        <table>
          <tbody>
            <tr>
              <td>Mapleview Groceries</td>
            </tr>
          </tbody>
        </table>
      </TableScroller>,
    );
    const box = document.querySelector(".table-scroll") as HTMLElement;
    stub(box, 1113, 350);

    fireEvent(window, new Event("resize"));
    const region = (await screen.findByRole("region", {
      name: "Transactions table",
    })) as HTMLElement;
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Scroll for actions/ })).toBeTruthy(),
    );
    expect(region.tabIndex).toBe(0);
    expect(box.scrollWidth - box.clientWidth).toBeGreaterThan(0);
  });
});

describe("M72 touch-target + safe-area source contracts", () => {
  const css = readFileSync(path.join(process.cwd(), "src", "index.css"), "utf8");
  const shell = readFileSync(
    path.join(process.cwd(), "src", "components", "layout", "AppShell.tsx"),
    "utf8",
  );
  // M185: MobileNav's six-tab bar is retired; shell/MobileTabBar owns it.
  const bottomNav = readFileSync(
    path.join(process.cwd(), "src", "components", "shell", "MobileTabBar.tsx"),
    "utf8",
  );
  // `viewport-fit=cover` is a DOCUMENT-level meta (index.html) — the shell
  // only consumes the env() insets it enables.
  const html = readFileSync(path.join(process.cwd(), "index.html"), "utf8");

  it("applies the 44px floor only below the md breakpoint", () => {
    const block = /@media \(max-width: 767\.98px\) \{([\s\S]*?)\n\}/.exec(css);
    expect(block).not.toBeNull();
    expect(block![1]).toContain("min-height: 44px");
    expect(block![1]).toContain("min-width: 44px");
    // The floor must be scoped: nothing above the media query raises heights.
    expect(css.slice(0, block!.index)).not.toContain("min-height: 44px");
  });

  it("extends the 44px floor to the nav drawer (PM ruling on QA F3)", () => {
    // The drawer sits outside `main`, so `main button/a` never reached it and
    // its rows measured 36px (menu) / 32px (accounts) at 390. The floor
    // follows the surface: `#app-nav` gets the same selectors, inside the SAME
    // < md block, so the desktop rail keeps M71's metrics.
    const block = /@media \(max-width: 767\.98px\) \{([\s\S]*?)\n\}/.exec(css);
    expect(block).not.toBeNull();
    expect(block![1]).toContain("#app-nav button");
    expect(block![1]).toContain("#app-nav a[href]");
    expect(block![1]).toContain("#app-nav input");
    // Nothing drawer-related may sit outside the media query (desktop parity).
    expect(css.slice(0, block!.index)).not.toContain("#app-nav");
    expect(css.slice(block!.index + block![0].length)).not.toContain("#app-nav");
  });

  it("clears the iOS status bar and the gesture bar", () => {
    expect(shell).toContain("env(safe-area-inset-top)");
    expect(shell).toContain("env(safe-area-inset-bottom)");
    expect(html).toContain("viewport-fit");
    expect(bottomNav).toContain("env(safe-area-inset-bottom)");
  });
});

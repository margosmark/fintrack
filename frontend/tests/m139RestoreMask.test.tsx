// M139 restore-seam mask contract (t_2d8ffedb P1) — the MASK half of a RESTORED
// session.
//
// `initMaskForSession()` is the boot/logout contract (src/lib/mask.ts): clear any
// stale sessionStorage key, then apply the caller's default. `applyUser` ran it
// for every EXPLICIT transition — but the session RESTORE path seeds `cacheOwner`
// directly (deliberately: a reload must not drop the cache it is repopulating)
// and so never ran it. A restored tab therefore kept a stale `fintrack.mask` key
// outliving the session it described, and skipped the read-only demo default, so
// the chip and the stored key disagreed.
//
// The two suites that cover the locked-vault/mask degradation
// (lockedVaultDegradation.test.ts, lockedVaultKnownGaps.test.tsx) are green and
// neither probes this seam — which is how the hole shipped with passing gates.
// This file pins the seam itself, and it renders the REAL AuthProvider: the chip
// suite mocks `useAuth`, so a mocked user can never exercise what a restore does.
//
// The three parts are one acceptance row (the post-restore screen must match the
// cold-boot contract): stale key cleared, read-only demo masked, chip ↔ key agree.
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../src/components/layout/AppShell";
import { AuthProvider, useAuth } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { DEMO_EMAIL } from "../src/auth/readOnlySession";
import {
  MASK_SESSION_KEY,
  MASK_TEXT,
  initMaskForSession,
  isMasked,
  setMasked,
  useMasked,
} from "../src/lib/mask";
import type { User } from "../src/types";

const mocks = vi.hoisted(() => ({
  restoreSession: vi.fn<() => Promise<unknown>>(),
  listAccounts: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, restoreSession: mocks.restoreSession };
});

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

// jsdom has no matchMedia; same query-aware stub the chip suite uses (any
// min-width query = true keeps the desktop rail, where the chip has a label).
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

const user = (email: string): User =>
  ({ id: "7", email, currency_tracker_enabled: false }) as User;

/** One seeded account: the rail prints its `total_value` through `formatMoney`. */
const ACCOUNT = {
  id: 1,
  institution_id: null,
  name: "Everyday",
  type: "chequing",
  currency: "CAD",
  taxable: false,
  llm_autopilot: false,
  cash_balance: "2500.00",
  investment_value: "0.00",
  total_value: "25000.00",
  starting_balance: "0.00",
  name_cipher: null,
};

/**
 * Every render of the mounted tree, as `(identity, mask)`.
 *
 * Recorded DURING render, so a render that had the restored identity on screen
 * while the store still read unmasked is on the record — that is the state the
 * M170 trace caught in the DOM (chip "Unmasked" + figures in the clear), and
 * "no unmasked flash" is only a measurement if it is taken per render.
 */
const renders: { user: string | null; masked: boolean }[] = [];

function RenderProbe() {
  const { user: current } = useAuth();
  const masked = useMasked();
  renders.push({ user: current?.email ?? null, masked });
  return null;
}

/** Every `$…` money callout currently on screen (the mask's job is to eat them). */
function moneyCallouts(root: HTMLElement): string[] {
  return [...root.querySelectorAll("*")]
    .flatMap((el) => [...el.childNodes])
    .filter((n): n is Text => n.nodeType === Node.TEXT_NODE)
    .map((n) => n.textContent ?? "")
    .flatMap((t) => t.match(/\$-?[\d,.]+[KMB]?/g) ?? []);
}

/** Mount the shell behind the REAL AuthProvider — the restore probe lives there. */
function renderRestoredShell() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RenderProbe />
        <ThemeProvider>
          <ToastProvider>
            <MemoryRouter initialEntries={["/dashboard"]}>
              <AppShell />
            </MemoryRouter>
          </ToastProvider>
        </ThemeProvider>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

/** The chip's own press state and the stored key, read together. */
async function chipAndKey() {
  const chip = await screen.findByTestId("mask-chip");
  return { pressed: chip.getAttribute("aria-pressed"), key: sessionStorage.getItem(MASK_SESSION_KEY) };
}

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
  renders.length = 0;
  mocks.listAccounts.mockResolvedValue({ items: [], total: 0 });
});

afterEach(() => {
  // Unmount BEFORE resetting the store: this hook runs before the setup file's
  // cleanup (vitest afterEach hooks are LIFO), and a store reset while a chip
  // is still mounted is an out-of-act update.
  cleanup();
  sessionStorage.clear();
  initMaskForSession();
  vi.clearAllMocks();
});

describe("the mask contract on a RESTORED session (t_2d8ffedb P1)", () => {
  it("clears a stale key when a non-demo session is restored", async () => {
    // The exact state the hole leaves behind: the previous session masked, the
    // tab re-enters through the refresh cookie with no memory of that choice.
    sessionStorage.setItem(MASK_SESSION_KEY, "1");

    mocks.restoreSession.mockResolvedValue({ user: user("mark@example.com"), access_token: "t" });
    renderRestoredShell();

    const { pressed, key } = await chipAndKey();
    // restore-on-load is OFF (PM ruling): the mask dies with the session, so the
    // stale key must not outlive it and the chip must agree with the key.
    expect(isMasked()).toBe(false);
    expect(key).toBeNull();
    expect(pressed).toBe("false");
  });

  it("applies the storefront default when a read-only demo session is restored", async () => {
    mocks.restoreSession.mockResolvedValue({ user: user(DEMO_EMAIL), access_token: "t" });
    renderRestoredShell();

    const { pressed, key } = await chipAndKey();
    // The demo tenant is a public storefront and boots masked (D5) — a restored
    // demo visit is still a boot, so it takes the same default an explicit
    // demo login takes.
    expect(isMasked()).toBe(true);
    expect(key).toBe("1");
    expect(pressed).toBe("true");
  });

  it("does not stomp a choice the user already made when the restore lands late", async () => {
    // The hazard the guard exists for, and the one this fix first got WRONG: a
    // restore resolves while the tab is already live, so it can land after the
    // user has acted. An unconditional re-init here reset a live mask to
    // unmasked — caught by tests/m167WorldMap.test.tsx, whose harness renders
    // the real AuthProvider and calls setMasked(true) before the probe answers.
    let release: (value: unknown) => void = () => {};
    mocks.restoreSession.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    renderRestoredShell();

    // The user masks before the probe answers (a slow or retried restore).
    await act(async () => {
      setMasked(true);
    });

    await act(async () => {
      release({ user: user("mark@example.com"), access_token: "t" });
    });

    // The late restore leaves it alone: the session default is a default, not a
    // lock, and the key still agrees with the chip.
    await waitFor(() => expect(screen.getByTestId("mask-chip").getAttribute("aria-pressed")).toBe("true"));
    expect(isMasked()).toBe(true);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBe("1");
  });

  it("keeps chip and key in agreement after a restore, both ways", async () => {
    for (const [email, expected] of [
      [DEMO_EMAIL, "true"],
      ["mark@example.com", "false"],
    ] as const) {
      sessionStorage.clear();
      initMaskForSession();
      mocks.restoreSession.mockResolvedValue({ user: user(email), access_token: "t" });
      const view = renderRestoredShell();
      await waitFor(async () => {
        const { pressed, key } = await chipAndKey();
        // Agreement is the invariant: the chip's own state and the session key
        // are two records of one fact, and neither may drift from the other.
        expect(pressed).toBe(expected);
        expect(key).toBe(expected === "true" ? "1" : null);
      });
      view.unmount();
    }
  });
});

// The chip is the state's RECORD; these are what the user actually reads. A
// restored demo session must render its seeded figures as bullets on the
// surfaced money (the rail prints every account's `total_value` through
// `formatMoney`), and the control beside it proves the surface is real — that
// the control's bullets are the mask and not a missing number.
describe("the mask contract on the SEEDED money surfaces of a restored session", () => {
  it("prints a restored demo session's balance as bullets, and never in the clear", async () => {
    mocks.listAccounts.mockResolvedValue({ items: [ACCOUNT], total: 1 });
    mocks.restoreSession.mockResolvedValue({ user: user(DEMO_EMAIL), access_token: "t" });

    const { container } = renderRestoredShell();

    // M185: the shell surface that prints account balances is the More panel
    // (the rail is retired), so the masked figures live behind it.
    fireEvent.click(await screen.findByTestId("more-toggle"));
    await screen.findByTestId("more-panel");
    const { key } = await chipAndKey();
    await waitFor(() => expect(container.textContent).toContain(MASK_TEXT));
    expect(key).toBe("1");
    // Not one dollar figure anywhere on the restored screen.
    expect(moneyCallouts(container)).toEqual([]);

    // No unmasked flash: every render that had the restored demo identity on
    // screen already carried the mask. A render with `masked: false` here is
    // exactly the M170 trace (identity present, store unmasked, figures in the
    // clear), which is the state this seam shipped with.
    const seen = renders.filter((record) => record.user === DEMO_EMAIL);
    expect(seen.length).toBeGreaterThan(0);
    for (const record of seen) expect(record.masked).toBe(true);
  });

  it("prints the same balance for an ordinary restored session", async () => {
    mocks.listAccounts.mockResolvedValue({ items: [ACCOUNT], total: 1 });
    mocks.restoreSession.mockResolvedValue({ user: user("mark@example.com"), access_token: "t" });

    const { container } = renderRestoredShell();

    // M185: the account balances live in the More panel now (rail retired).
    fireEvent.click(await screen.findByTestId("more-toggle"));
    await screen.findByTestId("more-panel");
    await waitFor(() => expect(container.textContent).toContain("$25,000.00"));
    expect(moneyCallouts(container)).toContain("$25,000.00");
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
  });
});

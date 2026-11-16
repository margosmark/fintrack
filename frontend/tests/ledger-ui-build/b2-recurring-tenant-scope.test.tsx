// B2 (QA t_b29beeaa blocker) — `fintrack.recurring-candidates.v1` is
// TAB-scoped storage, and a tab is not a tenant.
//
// The defect: nothing stamped the entry and `clearCandidates()` was never
// called by the auth seam, so the demo tenant's candidate rows (payees,
// amounts, transaction ids) survived logout and rendered for a different,
// zero-ledger tenant in the same tab — and survived a hard reload, because
// sessionStorage does.
//
// What is pinned here, at the seam that owns identity (`AuthProvider`) and at
// the storage module itself:
//   * an identity change (login / logout / failed boot / restore-as-someone-
//     else) leaves NO prior tenant's rows in storage;
//   * a restore as the SAME identity keeps the entry — the documented
//     "reloading the tab does not re-run the scan" contract;
//   * the card, mounted behind a RequireAuth-shaped gate, never renders the
//     previous tenant's rows — not even for one paint (no stale flash);
//   * a pre-fix unowned entry (bare array) is deleted on sight, never served.
import type { ReactNode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { restoreSession, login, logout, getRecurringCandidates, listBills } = vi.hoisted(() => ({
  restoreSession: vi.fn<() => Promise<import("../../src/types").AuthResponse | null>>(),
  login: vi.fn<() => Promise<import("../../src/types").AuthResponse>>(),
  logout: vi.fn<() => Promise<void>>(),
  getRecurringCandidates: vi.fn<() => Promise<unknown>>(),
  listBills: vi.fn<() => Promise<unknown[]>>(),
}));

vi.mock("../../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../src/api/client")>();
  return { ...actual, restoreSession, login, logout };
});
vi.mock("../../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../src/api/transactions")>()),
  getRecurringCandidates,
  acceptRecurringGroup: vi.fn(),
  suppressRecurringGroup: vi.fn(),
}));
vi.mock("../../src/api/bills", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../src/api/bills")>()),
  listBills,
}));
vi.mock("../../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), undo: vi.fn() }),
}));

import { AuthProvider, useAuth } from "../../src/auth/AuthContext";
import { RecurringPromptCard } from "../../src/components/transactions/RecurringPromptCard";
import type { RecurringCandidate } from "../../src/api/transactions";
import {
  candidatesOwner,
  readCandidates,
  scopeCandidatesTo,
  writeCandidates,
} from "../../src/lib/recurringSession";
import type { User } from "../../src/types";

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

function makeUser(id: string, email: string): User {
  return {
    id,
    email,
    name: null,
    privacy_mode: false,
    currency_tracker_enabled: true,
    llm_provider: "none",
    llm_base_url: null,
    llm_model: "",
    encryption_required: false,
    dashboard_preset: "overview",
    saved_report_views: [],
    recurring_intent_tag_ids: [],
  };
}

/**
 * NUMERIC ids on purpose: the wire sends the user id as an int
 * (`backend/app/schemas/users.py::UserPublic` → `id: int`, the model behind
 * login, demo login, the session restore and `GET /users/me`) even though
 * `types.User.id` is declared `string`, and that runtime shape is exactly what
 * the stamp has to survive — an un-normalized `1` vs `"1"` compared unequal
 * made a same-user reload drop its own cache (caught on the live stack, not in
 * a mock).
 */
const DEMO = makeUser(1 as unknown as string, "demo@fintracker.local");
const TENANT_B = makeUser(2 as unknown as string, "b@example.com");

const authPair = (user: User) => ({
  access_token: `token-${user.id}`,
  refresh_token: "unused",
  token_type: "bearer" as const,
  user,
});

function candidate(payee: string): RecurringCandidate {
  return {
    group_key: `${payee}|monthly`,
    dismiss_key: `${payee}|monthly`,
    payee,
    cadence: "monthly",
    occurrence_count: 4,
    transaction_ids: [11, 12, 13, 14],
    monthly_total: "-15.99",
    first_date: "2026-06-03",
    last_date: "2026-09-03",
    next_expected_date: "2026-10-03",
  };
}

const DEMO_ROWS = [candidate("Netflix"), candidate("Hydro One")];

/** Raw value under the key — the evidence QA reads out of devtools. */
function storedRaw(): string | null {
  try {
    return window.sessionStorage.getItem("fintrack.recurring-candidates.v1");
  } catch {
    return null;
  }
}

/** The RequireAuth shape: nothing renders until the probe resolved a user. */
function Gated({ children }: { children: ReactNode }) {
  const { ready, user } = useAuth();
  if (!ready) return <div data-testid="boot" />;
  if (user === null) return <div data-testid="login" />;
  return <>{children}</>;
}

function mountAuth(restoring: User | null) {
  restoreSession.mockResolvedValue(restoring === null ? null : authPair(restoring));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/transactions"]}>
      <QueryClientProvider client={client}>
        <AuthProvider>
          <Gated>
            <RecurringPromptCard />
          </Gated>
        </AuthProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  scopeCandidatesTo(null);
  window.sessionStorage.clear();
  listBills.mockResolvedValue([]);
});

describe("B2 — the recurring-candidate cache is tenant-scoped", () => {
  it("drops a foreign tenant's rows on an identity change, and keeps a same-identity entry", () => {
    scopeCandidatesTo(DEMO.id);
    writeCandidates(DEMO_ROWS);
    expect(readCandidates()?.map((g) => g.payee)).toEqual(["Netflix", "Hydro One"]);

    // Same identity, as after a reload of the SAME tab: the entry survives,
    // which is what stops the scan from re-running on every load.
    scopeCandidatesTo(DEMO.id);
    expect(readCandidates()).toEqual(DEMO_ROWS);

    // Another tenant binds the tab: their rows are gone from storage, not
    // merely hidden from one read.
    scopeCandidatesTo(TENANT_B.id);
    expect(readCandidates()).toBeNull();
    expect(storedRaw()).toBeNull();

    // ...and coming back does NOT resurrect what was dropped.
    scopeCandidatesTo(DEMO.id);
    expect(readCandidates()).toBeNull();

    // Logout binds "no session": with no identity there is no cache.
    writeCandidates(DEMO_ROWS);
    scopeCandidatesTo(null);
    expect(storedRaw()).toBeNull();
    expect(readCandidates()).toBeNull();
    expect(candidatesOwner()).toBeNull();
  });

  it("never serves — and deletes — a pre-fix unowned or corrupt entry", () => {
    // The exact pre-fix shape: a bare array under the key.
    window.sessionStorage.setItem(
      "fintrack.recurring-candidates.v1",
      JSON.stringify(DEMO_ROWS),
    );
    scopeCandidatesTo(TENANT_B.id);
    expect(readCandidates()).toBeNull();
    expect(storedRaw()).toBeNull();

    window.sessionStorage.setItem("fintrack.recurring-candidates.v1", "{not json");
    expect(readCandidates()).toBeNull();
    expect(storedRaw()).toBeNull();
  });

  it("one identity whether the wire sends 1 or \"1\" (users.id is an int)", () => {
    // Written under the numeric id the API really returns...
    scopeCandidatesTo(1);
    writeCandidates(DEMO_ROWS);
    expect(JSON.parse(storedRaw() ?? "{}").owner).toBe("1");

    // ...re-bound as the same int (a reload) and as a string: no churn.
    scopeCandidatesTo(1);
    expect(readCandidates()).toEqual(DEMO_ROWS);
    scopeCandidatesTo("1");
    expect(readCandidates()).toEqual(DEMO_ROWS);

    // A different id, in either spelling, is a different tenant.
    scopeCandidatesTo(2);
    expect(readCandidates()).toBeNull();
    expect(storedRaw()).toBeNull();
  });

  it("boots as the new tenant: no demo rows in storage, none on screen, no flash", async () => {
    window.sessionStorage.setItem(
      "fintrack.recurring-candidates.v1",
      JSON.stringify({ owner: DEMO.id, groups: DEMO_ROWS }),
    );
    // Hold the network OPEN: with the scan still in flight the card can only
    // show what it read from storage at mount, which is exactly the shape a
    // stale flash would take.
    getRecurringCandidates.mockReturnValue(new Promise(() => {}));

    mountAuth(TENANT_B);
    await screen.findByTestId("boot");
    await waitFor(() => expect(screen.queryByTestId("boot")).toBeNull());

    // The gate is open (tenant B resolved) and the demo's rows were purged by
    // the probe — before the card could mount and read them.
    expect(screen.queryByTestId("login")).toBeNull();
    expect(storedRaw()).toBeNull();
    expect(screen.queryByTestId("recurring-prompt")).toBeNull();
    expect(screen.queryByText("Netflix")).toBeNull();
    expect(screen.queryByText("Hydro One")).toBeNull();
    // The seam BINDS the identity: this is what makes every later read
    // attributable. Remove the AuthContext call and this stays null — the
    // cache would then be stamped "no session" for everybody.
    expect(candidatesOwner()).toBe(String(TENANT_B.id));
  });

  it("the new tenant's own scan writes THEIR rows — never the demo's", async () => {
    window.sessionStorage.setItem(
      "fintrack.recurring-candidates.v1",
      JSON.stringify({ owner: DEMO.id, groups: DEMO_ROWS }),
    );
    getRecurringCandidates.mockResolvedValue({ groups: [] });

    mountAuth(TENANT_B);
    await waitFor(() => expect(readCandidates()).toEqual([]));

    const raw = storedRaw();
    expect(raw).not.toBeNull();
    expect(raw).not.toContain("Netflix");
    // Stamped with THEIR identity, normalized to the one canonical form.
    expect(JSON.parse(raw ?? "{}").owner).toBe(String(TENANT_B.id));
  });

  it("same-user flow still works: a restored session reuses its own entry without a scan", async () => {
    window.sessionStorage.setItem(
      "fintrack.recurring-candidates.v1",
      JSON.stringify({ owner: DEMO.id, groups: DEMO_ROWS }),
    );
    getRecurringCandidates.mockResolvedValue({ groups: [] });

    mountAuth(DEMO);
    await waitFor(() => expect(screen.queryByTestId("boot")).toBeNull());

    expect(screen.getByTestId("recurring-prompt")).toBeTruthy();
    expect(screen.getByText("Netflix")).toBeTruthy();
    expect(getRecurringCandidates).not.toHaveBeenCalled();
    expect(readCandidates()).toHaveLength(2);
    expect(candidatesOwner()).toBe(String(DEMO.id));
  });

  it("a failed boot clears the tab's cache: no session, no stored rows", async () => {
    window.sessionStorage.setItem(
      "fintrack.recurring-candidates.v1",
      JSON.stringify({ owner: DEMO.id, groups: DEMO_ROWS }),
    );
    mountAuth(null);
    await waitFor(() => expect(screen.queryByTestId("boot")).toBeNull());

    expect(screen.getByTestId("login")).toBeTruthy();
    expect(storedRaw()).toBeNull();
    expect(readCandidates()).toBeNull();
    expect(candidatesOwner()).toBeNull();
  });
});

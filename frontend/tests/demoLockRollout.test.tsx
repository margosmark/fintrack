// demoLockRollout — F-2 / F-3 / F-4 pins against docs/47 §4 and §13
// (acceptance examples 2/3/4 as amended by the security follow-up wave).
//
// What is pinned is the CONTRACT of a refused demo write, not the styling:
//
//   * every control §13 names wears `demo-lock` on a read-only session and
//     binds itself (aria-describedby) to a VISIBLE reason — never `sr-only`,
//     never a 1×1px clip (F-4 measured exactly that on the M185 candidate);
//   * the REAL `403 demo_account_restricted` renders `demo-refusal`
//     (`role="status"`) + exactly one `toast.info` and NEVER an error —
//     zero `role="alert"` elements, no red error line (F-2's measured repro
//     was `Save export schedule` raising `role="alert"`);
//   * a live tenant renders ZERO padlocks (§13 ex.1);
//   * the staleness strip's refresh affordance wears the same lock and its
//     refusal renders even when the header button does not exist
//     (`showHeader={false}` on /accounts/{id}) — F-3.
//
// The Investments section drives the real page: real AuthProvider (demo
// session resolved from mocked `restoreSession`), real ToastProvider, real
// React Query, real DemoLock — only the API seam is mocked.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getSchedule, listArtifacts, putSchedule } from "../src/api/backup";
import { getExportSchedule, putExportSchedule } from "../src/api/export";
import { runScheduledBackup } from "../src/lib/scheduledBackup";
import { ScheduledBackupPanel } from "../src/components/ScheduledBackupPanel";
import {
  ScheduledExportPanel,
  EXPORT_SCHEDULE_REASON_ID,
} from "../src/components/ScheduledExportPanel";
import {
  BACKUP_NOW_REASON_ID,
  BACKUP_SCHEDULE_REASON_ID,
} from "../src/components/ScheduledBackupPanel";
import { ToastProvider } from "../src/components/ui/Toast";
import { AuthProvider } from "../src/auth/AuthContext";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Investments } from "../src/pages/Investments";

// --- hoisted shared state ---------------------------------------------------

/** The session the stubbed `useAuth` hands out; `null` = signed out. */
const session = vi.hoisted(() => ({
  user: null as { id: string; email: string } | null,
}));
/** The vault's in-memory lock state (`lib/vault.isUnlocked`). */
const vault = vi.hoisted(() => ({ locked: true }));

const DEMO_USER = { id: "1", email: "demo@fintracker.local" };
const LIVE_USER = { id: "9", email: "mark@example.com" };

/** The envelope the server really answers a refused demo write with. */
const DEMO_403 = {
  code: "demo_account_restricted",
  detail: "The demo account is read-only; changes are not allowed",
};

// --- module seams -----------------------------------------------------------

// docs/47 §4: the guard reads the session through `useAuth`; stub ONLY that
// export so every other binding (AuthProvider, authUser scope) stays real.
vi.mock("../src/auth/AuthContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/auth/AuthContext")>();
  return {
    ...actual,
    useAuth: () => ({ user: session.user, setUser: () => undefined }),
  };
});

vi.mock("../src/api/backup", () => ({
  ARTIFACTS_PATH: "/backup/artifacts",
  SCHEDULE_PATH: "/backup/schedule",
  deleteArtifact: vi.fn(),
  downloadArtifact: vi.fn(),
  getArtifact: vi.fn(),
  getSchedule: vi.fn(),
  listArtifacts: vi.fn(),
  putSchedule: vi.fn(),
  restoreBackup: vi.fn(),
  uploadArtifact: vi.fn(),
}));

vi.mock("../src/api/export", () => ({
  EXPORT_SCHEDULE_PATH: "/export/schedule",
  downloadOfxExport: vi.fn(),
  fetchZip: vi.fn(),
  getExportSchedule: vi.fn(),
  getFullExport: vi.fn(),
  putExportSchedule: vi.fn(),
}));

vi.mock("../src/lib/vault", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/vault")>();
  return { ...actual, isUnlocked: () => !vault.locked };
});

vi.mock("../src/lib/scheduledBackup", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/scheduledBackup")>();
  return { ...actual, runScheduledBackup: vi.fn() };
});

// --- Investments page seams (modeled on m146's harness) --------------------

const h = vi.hoisted(() => {
  const user = (email: string) => ({
    id: email === "demo@fintracker.local" ? "1" : "9",
    email,
    privacy_mode: false,
    currency_tracker_enabled: false,
    llm_provider: "openai",
    llm_base_url: null,
    llm_model: "gpt-4o-mini",
    encryption_required: false,
    dashboard_preset: "overview",
    saved_report_views: [],
    recurring_intent_tag_ids: [],
  });
  const brokerage = {
    id: 1,
    institution_id: null,
    name: "TFSA",
    type: "tfsa",
    currency: "CAD",
    taxable: false,
    llm_autopilot: false,
    cash_balance: "0.00",
    investment_value: "0.00",
    total_value: "0.00",
    starting_balance: "0.00",
    name_cipher: null,
  };
  const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 200, offset: 0 });
  /** One STALE positions row — the condition for the strip to render. */
  const stalePosition = {
    account_id: 1,
    account_name: "TFSA",
    security_id: 7,
    symbol: "MSFT",
    listing_currency: "CAD",
    quantity: "10",
    price_date: "2026-09-20",
    price: "100.00",
    price_source: "yfinance",
    price_age_days: 9,
    price_stale: true,
    book_cost_cad: "900.00",
    spark: ["95", "97", "100"],
    market_value_native: "1000.00",
    market_value_cad: "1000.00",
    fx_rate_to_cad: "1",
    previous_close_native: "99.00",
    previous_close_date: "2026-09-19",
    day_change_native: "1.00",
    day_change_pct: 1.01,
    day_change_cad: "10.00",
    unrealized_gain_cad: "100.00",
    unrealized_gain_native: "100.00",
    unrealized_gain_pct: 11.11,
  };
  return {
    restoreSession: vi.fn(),
    me: vi.fn(),
    listAccounts: vi.fn(),
    getPositions: vi.fn(),
    getPortfolioValueHistory: vi.fn(),
    getWatchlist: vi.fn(),
    listSecurities: vi.fn(),
    listTrades: vi.fn(),
    getDividendSuggestions: vi.fn(),
    getExposure: vi.fn(),
    getBreakdown: vi.fn(),
    getSecurityOverview: vi.fn(),
    refreshPrices: vi.fn(),
    user,
    brokerage,
    page,
    stalePosition,
  };
});

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, restoreSession: h.restoreSession, me: h.me };
});
vi.mock("../src/api/accounts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/accounts")>();
  const { useQuery } = await import("@tanstack/react-query");
  return {
    ...actual,
    listAccounts: h.listAccounts,
    useInvestmentAccounts: () => {
      const accounts = useQuery({
        queryKey: ["accounts", 200],
        queryFn: () => h.listAccounts(200),
      });
      const investmentAccounts = (accounts.data?.items ?? []).filter(
        (a) => actual.INVESTMENT_ACCOUNT_TYPES[a.type] === true,
      );
      return { accounts, investmentAccounts };
    },
  };
});
vi.mock("../src/api/portfolio", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/portfolio")>();
  return {
    ...actual,
    getPositions: h.getPositions,
    getPortfolioValueHistory: h.getPortfolioValueHistory,
    getExposure: h.getExposure,
    getBreakdown: h.getBreakdown,
    refreshPrices: h.refreshPrices,
  };
});
vi.mock("../src/api/watchlist", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/watchlist")>();
  return { ...actual, getWatchlist: h.getWatchlist };
});
vi.mock("../src/api/securities", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/securities")>();
  return { ...actual, listSecurities: h.listSecurities, getSecurityOverview: h.getSecurityOverview };
});
vi.mock("../src/api/trades", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/trades")>();
  return {
    ...actual,
    listTrades: h.listTrades,
    getDividendSuggestions: h.getDividendSuggestions,
  };
});

/** jsdom has no matchMedia (ThemeProvider + reduced-motion probes read it). */
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

class FakeEventSource {
  url: string;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  constructor(url: string) {
    this.url = url;
  }
  close(): void {}
}

// --- harness ----------------------------------------------------------------

const SCHEDULE = {
  frequency: "off",
  hour: 3,
  weekday: 0,
  day_of_month: 1,
  keep_last_n: 5,
} as const;

function providers(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ToastProvider>{ui}</ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>
  );
}

function renderExportPanel() {
  return render(providers(<ScheduledExportPanel userId="1" />));
}

function renderBackupPanel() {
  return render(providers(<ScheduledBackupPanel />));
}

/** The real page: real AuthProvider (boot resolves the mocked session),
 *  real ToastProvider, real React Query — only the API seam is mocked. */
function renderPage(user: { id: string; email: string } | null, ui: React.ReactNode) {
  session.user = user;
  h.restoreSession.mockResolvedValue(
    user === null
      ? Promise.reject(new Error("signed out"))
      : { access_token: "token", token_type: "bearer", user: h.user(user.email) },
  );
  h.me.mockResolvedValue(user === null ? undefined : h.user(user.email));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/investments"]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <ToastProvider>
            <AuthProvider>{ui}</AuthProvider>
          </ToastProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** How many error-shaped things are on screen right now. */
function errorAlerts(): HTMLElement[] {
  return Array.from(document.querySelectorAll('[role="alert"]'));
}

/** The reason element a control's `aria-describedby` names. */
function describedReason(control: HTMLElement): HTMLElement {
  const id = control.getAttribute("aria-describedby");
  expect(id).toBeTruthy();
  const el = document.getElementById(id as string);
  expect(el).not.toBeNull();
  return el as HTMLElement;
}

/** VISIBLE: the reason must not be the sr-only clip F-4 measured. */
function expectVisibleReason(el: HTMLElement): void {
  expect(el.className).not.toContain("sr-only");
  expect(el.textContent ?? "").toContain("read-only");
}

beforeEach(() => {
  session.user = DEMO_USER;
  vault.locked = true;
  localStorage.clear();
  sessionStorage.clear();
  vi.mocked(getSchedule).mockResolvedValue({ ...SCHEDULE });
  vi.mocked(listArtifacts).mockResolvedValue([]);
  vi.mocked(getExportSchedule).mockResolvedValue({ ...SCHEDULE });
  vi.mocked(putSchedule).mockResolvedValue({ ...SCHEDULE });
  vi.mocked(putExportSchedule).mockResolvedValue({ ...SCHEDULE });
  vi.mocked(runScheduledBackup).mockResolvedValue("uploaded");
  h.listAccounts.mockResolvedValue(h.page([h.brokerage]));
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([]);
  h.getExposure.mockResolvedValue({ items: [], total: 0 });
  h.getBreakdown.mockResolvedValue({
    dimension: "sector",
    items: [],
    total_cad: "0",
    unclassified_cad: "0",
  });
  h.getPortfolioValueHistory.mockResolvedValue({
    account_ids: [1],
    from: "2026-04-10",
    to: "2026-10-10",
    granularity: "day",
    requested_granularity: "day",
    currency: "CAD",
    native_currency: "CAD",
    min_event_value: "0",
    has_data: false,
    buckets: [],
    events: [],
    stats: {
      from_bucket: null,
      to_bucket: null,
      from_value_cad: null,
      to_value_cad: null,
      change_cad: null,
      change_pct: null,
    },
  });
  h.getPositions.mockResolvedValue({
    items: [h.stalePosition],
    total: 1,
    portfolio_value_cad: "1000.00",
    total_day_change_cad: "10.00",
    total_day_change_pct: 1.01,
    day_change_as_of: "2026-09-19",
    day_change_mixed_baseline: false,
  });
  h.refreshPrices.mockRejectedValue({ ...DEMO_403 });
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  session.user = null;
});

// --- §13 ex.4: Settings' two schedule forms ---------------------------------

describe("docs/47 §13 ex.4 — Export schedule → Save (F-2 measured repro)", () => {
  it("demo: the Save control wears the padlock bound to a VISIBLE reason", async () => {
    renderExportPanel();
    const save = await screen.findByRole("button", { name: /Save export schedule/ });
    expect(save.querySelector('[data-testid="demo-lock"]')).not.toBeNull();
    expect(save.getAttribute("aria-describedby")).toBe(EXPORT_SCHEDULE_REASON_ID);
    expectVisibleReason(describedReason(save));
  });

  it("demo: the real 403 renders demo-refusal + one info toast, NEVER role=alert", async () => {
    vi.mocked(putExportSchedule).mockRejectedValue({ ...DEMO_403 });
    renderExportPanel();
    const save = await screen.findByRole("button", { name: /Save export schedule/ });
    fireEvent.click(save);

    const refusals = await screen.findAllByTestId("demo-refusal");
    // ONE refusal element per page (docs/47 §4 / docs/09 §Read-only #3).
    expect(refusals).toHaveLength(1);
    expect(refusals[0].getAttribute("role")).toBe("status");
    expect(refusals[0].textContent).toContain("read-only");
    // The one toast.info — and nothing error-shaped anywhere in the DOM.
    expect(
      screen.getByText("Save export schedule — the demo account is read-only"),
    ).toBeTruthy();
    expect(errorAlerts()).toHaveLength(0);
    expect(screen.queryByText("Could not save schedule")).toBeNull();
    // The write really went out (the boundary stays server-authoritative).
    expect(vi.mocked(putExportSchedule)).toHaveBeenCalledTimes(1);
  });

  it("live tenant: ZERO padlocks and no reason binding (§13 ex.1)", async () => {
    session.user = LIVE_USER;
    renderExportPanel();
    const save = await screen.findByRole("button", { name: /Save export schedule/ });
    expect(save.querySelector('[data-testid="demo-lock"]')).toBeNull();
    expect(save.getAttribute("aria-describedby")).toBeNull();
    expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(0);
  });
});

describe("docs/47 §13 ex.4 — Back up now (F-2)", () => {
  it("demo + unlocked vault: the padlock renders and a refused run is a refusal, not an error", async () => {
    vault.locked = false;
    vi.mocked(runScheduledBackup).mockRejectedValue({ ...DEMO_403 });
    renderBackupPanel();

    const now = await screen.findByRole("button", { name: /Back up now/ });
    expect(now.getAttribute("disabled")).toBeNull();
    expect(now.querySelector('[data-testid="demo-lock"]')).not.toBeNull();
    expect(now.getAttribute("aria-describedby")).toBe(BACKUP_NOW_REASON_ID);
    expectVisibleReason(describedReason(now));

    fireEvent.click(now);
    const refusals = await screen.findAllByTestId("demo-refusal");
    expect(refusals).toHaveLength(1);
    expect(refusals[0].getAttribute("role")).toBe("status");
    expect(screen.getByText("Back up now — the demo account is read-only")).toBeTruthy();
    expect(errorAlerts()).toHaveLength(0);
    expect(screen.queryByText("Backup failed")).toBeNull();
  });

  it("demo + LOCKED vault: no padlock — VaultLocked explains the disabled control (§4)", async () => {
    vault.locked = true;
    renderBackupPanel();
    const now = await screen.findByRole("button", { name: /Back up now/ });
    expect(now.getAttribute("disabled")).not.toBeNull();
    expect(now.querySelector('[data-testid="demo-lock"]')).toBeNull();
    expect(now.getAttribute("aria-describedby")).toBeNull();
    // The E2EE reason still explains the control in place.
    expect(screen.getByText(/Unlock the vault to run this here/)).toBeTruthy();
  });

  it("demo: Save backup schedule wears the padlock too (same panel, same rule)", async () => {
    renderBackupPanel();
    const save = await screen.findByRole("button", { name: /Save backup schedule/ });
    expect(save.querySelector('[data-testid="demo-lock"]')).not.toBeNull();
    expect(save.getAttribute("aria-describedby")).toBe(BACKUP_SCHEDULE_REASON_ID);
    expectVisibleReason(describedReason(save));
  });

  it("live tenant: ZERO padlocks in the panel (§13 ex.1)", async () => {
    session.user = LIVE_USER;
    renderBackupPanel();
    const save = await screen.findByRole("button", { name: /Save backup schedule/ });
    expect(save.querySelector('[data-testid="demo-lock"]')).toBeNull();
    expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(0);
  });
});

// --- F-3/F-4: the staleness strip's refresh affordance ----------------------

describe("staleness strip refresh (F-3/F-4) — one verdict, both affordances", () => {
  it("demo on /accounts/{id} (no header): the strip wears the lock and its refusal renders", async () => {
    renderPage(DEMO_USER, <Investments accountId={1} showHeader={false} />);

    const strip = await screen.findByTestId("stale-strip-refresh");
    expect(strip.querySelector('[data-testid="demo-lock"]')).not.toBeNull();
    expect(strip.getAttribute("aria-describedby")).toBe("stale-strip-refresh-reason");
    const reason = describedReason(strip);
    expectVisibleReason(reason);
    // The header button does NOT exist on this route — the refusal must not
    // live inside it (F-3's measured gap).
    expect(screen.queryByTestId("refresh-prices")).toBeNull();

    fireEvent.click(strip);
    const refusals = await screen.findAllByTestId("demo-refusal");
    expect(refusals).toHaveLength(1);
    expect(refusals[0].getAttribute("role")).toBe("status");
    expect(errorAlerts()).toHaveLength(0);
    expect(screen.getByText("Refresh prices — the demo account is read-only")).toBeTruthy();
    // The request really went out.
    expect(h.refreshPrices).toHaveBeenCalledTimes(1);
  });

  it("demo with the header: BOTH affordances carry the same verdict and a reason each", async () => {
    renderPage(DEMO_USER, <Investments />);

    const header = await screen.findByTestId("refresh-prices");
    const strip = await screen.findByTestId("stale-strip-refresh");
    expect(header.querySelector('[data-testid="demo-lock"]')).not.toBeNull();
    expect(strip.querySelector('[data-testid="demo-lock"]')).not.toBeNull();
    expectVisibleReason(describedReason(header));
    expectVisibleReason(describedReason(strip));
    // Two controls, two reason elements — one verdict (same wording).
    expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(2);
  });

  it("live tenant: neither refresh affordance wears a padlock", async () => {
    renderPage(LIVE_USER, <Investments />);
    const header = await screen.findByTestId("refresh-prices");
    await screen.findByTestId("stale-strip-refresh");
    expect(header.querySelector('[data-testid="demo-lock"]')).toBeNull();
    expect(document.querySelectorAll('[data-testid="demo-lock"]')).toHaveLength(0);
    expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(0);
  });
});

// --- F-6: identifier-carrying storage keys are tenant-scoped ---------------

describe("F-6 — browser storage never carries another tenant's identifiers", () => {
  it("dismissing a dividend suggestion writes the SCOPED key, never the bare one", async () => {
    h.getDividendSuggestions.mockResolvedValue([
      {
        security_id: 7,
        account_id: 1,
        symbol: "MSFT",
        date: "2026-10-01",
        amount: "1.23",
        quantity: "10",
        gross_amount: "12.30",
        currency: "CAD",
      },
    ]);
    renderPage(DEMO_USER, <Investments accountId={1} />);

    const dismissAll = await screen.findByRole("button", { name: "Dismiss all" });
    fireEvent.click(dismissAll);

    // The pre-fix key is never written; the scoped key holds the triple.
    expect(localStorage.getItem("fintrack:dismissed_dividends")).toBeNull();
    const scoped = localStorage.getItem("fintrack:dismissed_dividends.1");
    expect(scoped).not.toBeNull();
    expect(JSON.parse(scoped as string)).toContain("1-7-2026-10-01");
    // And the dismissal took effect for THIS tenant.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Dismiss all" })).toBeNull(),
    );
  });
});

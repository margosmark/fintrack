// demoRefusalSettings — docs/47 §4 for the Settings mutations (F-2 residual,
// card t_a51c9b96, item 3).
//
// The absolute pin: a `demo_account_restricted` response NEVER renders
// `role="alert"` or an `error-panel`. Toast renders an error tone AS
// `role="alert"`, so every unguarded `toast.error(...)` for that one code was
// a violation.
//
// Pinned here:
//   * Save profile / Save (LLM) / the privacy switch each wear `demo-lock`
//     with `aria-describedby` → a VISIBLE `DemoReason` (never `sr-only`), and
//     stay ENABLED on the demo tenant (§4 — an inert control cannot explain
//     itself);
//   * their REAL 403 renders exactly ONE `demo-refusal` (`role="status"`) +
//     ONE `toast.info`, zero `role="alert"`, zero `error-panel`;
//   * `Test connection` keeps the W2 treatment — disabled with its own
//     visible reason, NO padlock (docs/47 §4 table);
//   * a LIVE tenant renders zero padlocks and zero reasons (§13 ex.1).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";

/** The session BOTH scopes hand out (they must never disagree). */
const session = vi.hoisted(() => ({
  user: null as { id: string; email: string } | null,
}));

const m = vi.hoisted(() => ({
  me: vi.fn(),
  updateProfile: vi.fn(),
  changePassword: vi.fn(),
  deleteAccount: vi.fn(),
  updateMe: vi.fn(),
  testLlm: vi.fn(),
  listCategories: vi.fn(),
  createCategory: vi.fn(),
  deleteCategory: vi.fn(),
  updateCategory: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  me: m.me,
}));
vi.mock("../src/api/users", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/users")>()),
  updateProfile: m.updateProfile,
  changePassword: m.changePassword,
  deleteAccount: m.deleteAccount,
  updateMe: m.updateMe,
  testLlm: m.testLlm,
}));
// PrivacySection renders the two SCHEDULED panels (Settings' Privacy card),
// which own their own queries — unmocked they would fail and paint an
// ErrorPanel (`role="alert"`) that has nothing to do with a demo refusal.
vi.mock("../src/api/backup", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/backup")>()),
  getSchedule: vi.fn(),
  listArtifacts: vi.fn(),
}));
vi.mock("../src/api/export", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/export")>()),
  getExportSchedule: vi.fn(),
}));
vi.mock("../src/api/categories", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/categories")>()),
  listCategories: m.listCategories,
  createCategory: m.createCategory,
  deleteCategory: m.deleteCategory,
  updateCategory: m.updateCategory,
}));
vi.mock("../src/auth/AuthContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/auth/AuthContext")>();
  return {
    ...actual,
    useAuth: () => ({ user: session.user, setUser: () => undefined }),
  };
});
vi.mock("../src/auth/authUser", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/auth/authUser")>();
  return { ...actual, useOptionalUser: () => session.user };
});

import type { User } from "../src/types";
import { getSchedule, listArtifacts } from "../src/api/backup";
import { getExportSchedule } from "../src/api/export";
import { ToastProvider } from "../src/components/ui/Toast";
import { AccountSection, LlmForm, PrivacySection, CategoryManager } from "../src/pages/Settings";

function user(email: string): User {
  return {
    id: email === "demo@fintracker.local" ? "demo" : "user-1",
    email,
    privacy_mode: false,
    currency_tracker_enabled: false,
    llm_provider: "none",
    llm_base_url: null,
    llm_model: "",
    encryption_required: false,
  } as User;
}

const DEMO_USER = user("demo@fintracker.local");
const LIVE_USER = user("mark@example.com");

/** The envelope the server really answers a refused demo write with. */
const DEMO_403 = {
  code: "demo_account_restricted",
  detail: "The demo account is read-only; changes are not allowed",
};

function renderSection(ui: ReactElement): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ToastProvider>{ui}</ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function errorAlerts(): HTMLElement[] {
  return Array.from(document.querySelectorAll('[role="alert"]'));
}

/** ONE refusal element (`role="status"`) + the ONE toast.info, nothing else. */
async function expectRefusal(action: string, apiMock: { mock: { calls: unknown[][] } }) {
  const refusals = await screen.findAllByTestId("demo-refusal");
  expect(refusals).toHaveLength(1);
  expect(refusals[0].getAttribute("role")).toBe("status");
  expect(screen.getByText(`${action} — the demo account is read-only`)).toBeTruthy();
  expect(errorAlerts()).toHaveLength(0);
  expect(screen.queryByTestId("error-panel")).toBeNull();
  await waitFor(() => expect(apiMock.mock.calls.length).toBeGreaterThan(0));
}

/** §4: padlock on the control, bound to a VISIBLE reason in the same panel. */
function expectLocked(control: HTMLElement, reasonId: string): void {
  const holder = control.querySelector('[data-testid="demo-lock"]') ? control : control.parentElement;
  expect(holder?.querySelector('[data-testid="demo-lock"]') ?? null).not.toBeNull();
  expect(control.getAttribute("aria-describedby")).toBe(reasonId);
  const reason = document.getElementById(reasonId);
  expect(reason).not.toBeNull();
  expect(reason?.getAttribute("data-testid")).toBe("demo-reason");
  expect(reason?.className ?? "").not.toContain("sr-only");
  expect(reason?.textContent ?? "").toContain("read-only");
}

function expectNoLocks(): void {
  expect(document.querySelectorAll('[data-testid="demo-lock"]')).toHaveLength(0);
  expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(0);
}

beforeEach(() => {
  vi.clearAllMocks();
  session.user = DEMO_USER;
  localStorage.clear();
  sessionStorage.clear();
  m.me.mockResolvedValue(DEMO_USER);
  m.updateProfile.mockResolvedValue(DEMO_USER);
  m.changePassword.mockResolvedValue({ access_token: "t", token_type: "bearer", user: DEMO_USER });
  m.deleteAccount.mockResolvedValue(undefined);
  m.updateMe.mockResolvedValue(DEMO_USER);
  m.testLlm.mockResolvedValue({ ok: true });
  m.listCategories.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  m.createCategory.mockResolvedValue({ id: 5, user_id: "demo", name: "New", parent_id: null, is_system: false });
  m.deleteCategory.mockResolvedValue(undefined);
  m.updateCategory.mockResolvedValue({});
  vi.mocked(getSchedule).mockResolvedValue({ frequency: "off", hour: 3, weekday: 0, day_of_month: 1, keep_last_n: 5 });
  vi.mocked(listArtifacts).mockResolvedValue([]);
  vi.mocked(getExportSchedule).mockResolvedValue({ frequency: "off", hour: 3, weekday: 0, day_of_month: 1, keep_last_n: 5 });
});

afterEach(() => {
  vi.clearAllMocks();
  session.user = null;
});

describe("docs/47 §4 — AccountSection (profile / password / delete)", () => {
  it("demo: Save profile wears the padlock bound to a VISIBLE reason and is enabled by VALIDATION alone", async () => {
    renderSection(<AccountSection />);
    const save = (await screen.findByRole("button", { name: /Save profile/ })) as HTMLElement;
    // Its only gate is `!canSaveProfile` — nothing is disabled FOR the demo.
    expectLocked(save, "account-demo-reason");
    const name = screen.getByLabelText("Display name");
    expect(name.hasAttribute("disabled")).toBe(false);
    fireEvent.change(name, { target: { value: "Demo Person" } });
    expect(save.hasAttribute("disabled")).toBe(false);
    // The demo notice no longer claims the form is disabled (§4) — it sits
    // beside the lock's reason, both visible.
    expect(screen.getAllByText(/The demo account is read-only/).length).toBeGreaterThan(0);
  });

  it("demo: a refused profile save is ONE demo-refusal + ONE toast.info, never role=alert", async () => {
    m.updateProfile.mockRejectedValue({ ...DEMO_403 });
    renderSection(<AccountSection />);

    const name = await screen.findByLabelText("Display name");
    fireEvent.change(name, { target: { value: "Demo Person" } });
    const save = screen.getByRole("button", { name: /Save profile/ });
    fireEvent.click(save);

    await expectRefusal("Save profile", m.updateProfile);
    // The form stays LIVE on the demo tenant (§4: a locked control is enabled).
    expect(name.hasAttribute("disabled")).toBe(false);
  });

  it("live tenant: ZERO padlocks and ZERO reasons (§13 ex.1)", async () => {
    session.user = LIVE_USER;
    m.me.mockResolvedValue(LIVE_USER);
    renderSection(<AccountSection />);
    await screen.findByRole("button", { name: /Save profile/ });
    expectNoLocks();
  });
});

describe("docs/47 §4 — LlmForm (Save + the W2 exception)", () => {
  it("demo: Save wears the padlock bound to a VISIBLE reason", async () => {
    renderSection(<LlmForm />);
    const save = await screen.findByRole("button", { name: /^Save$/ });
    expect(save.hasAttribute("disabled")).toBe(false);
    expectLocked(save, "llm-save-demo-reason");
  });

  it("demo: a refused save is ONE demo-refusal + ONE toast.info, never role=alert", async () => {
    m.updateMe.mockRejectedValue({ ...DEMO_403 });
    renderSection(<LlmForm />);

    const save = await screen.findByRole("button", { name: /^Save$/ });
    fireEvent.click(save);

    await expectRefusal("Save LLM settings", m.updateMe);
  });

  it("demo: Test connection keeps W2 — disabled with its own reason, NO padlock (docs/47 §4)", async () => {
    renderSection(<LlmForm />);
    const test = await screen.findByRole("button", { name: /Test connection/ });
    expect(test.hasAttribute("disabled")).toBe(true);
    expect(test.getAttribute("aria-describedby")).toBe("llm-test-disabled-reason");
    expect(test.querySelector('[data-testid="demo-lock"]')).toBeNull();
    const reason = document.getElementById("llm-test-disabled-reason");
    expect(reason).not.toBeNull();
    expect(reason?.textContent ?? "").toContain("demo");
    // W2: the probe never leaves the browser on the demo tenant.
    expect(m.testLlm).not.toHaveBeenCalled();
  });

  it("live tenant: ZERO padlocks and ZERO reasons", async () => {
    session.user = LIVE_USER;
    m.me.mockResolvedValue(LIVE_USER);
    renderSection(<LlmForm />);
    await screen.findByRole("button", { name: /^Save$/ });
    expectNoLocks();
  });
});

describe("docs/47 §4 — PrivacySection (privacy mode switch)", () => {
  it("demo: the switch wears the padlock bound to a VISIBLE reason and stays enabled", async () => {
    renderSection(<PrivacySection />);
    const toggle = (await screen.findByRole("switch", { name: "Privacy mode" })) as HTMLElement;
    expect(toggle.hasAttribute("disabled")).toBe(false);
    expectLocked(toggle, "privacy-mode-demo-reason");
  });

  it("demo: a refused flip is ONE demo-refusal + ONE toast.info, never role=alert", async () => {
    m.updateMe.mockRejectedValue({ ...DEMO_403 });
    renderSection(<PrivacySection />);

    const toggle = await screen.findByRole("switch", { name: "Privacy mode" });
    fireEvent.click(toggle);

    await expectRefusal("Change privacy mode", m.updateMe);
  });

  it("live tenant: ZERO padlocks and ZERO reasons", async () => {
    session.user = LIVE_USER;
    m.me.mockResolvedValue(LIVE_USER);
    renderSection(<PrivacySection />);
    await screen.findByRole("switch", { name: "Privacy mode" });
    expectNoLocks();
  });
});

describe("docs/47 §4 — CategoryManager (create)", () => {
  it("demo: Add wears the padlock bound to a VISIBLE reason", async () => {
    renderSection(<CategoryManager />);
    const name = await screen.findByLabelText(/New category name/);
    fireEvent.change(name, { target: { value: "Coffee" } });
    const add = screen.getByRole("button", { name: /^Add$/ });
    expect(add.hasAttribute("disabled")).toBe(false);
    expectLocked(add, "categories-demo-reason");
  });

  it("demo: a refused create is ONE demo-refusal + ONE toast.info, never role=alert", async () => {
    m.createCategory.mockRejectedValue({ ...DEMO_403 });
    renderSection(<CategoryManager />);

    const name = await screen.findByLabelText(/New category name/);
    fireEvent.change(name, { target: { value: "Coffee" } });
    fireEvent.click(screen.getByRole("button", { name: /^Add$/ }));

    await expectRefusal("Create category", m.createCategory);
  });

  it("live tenant: ZERO padlocks and ZERO reasons", async () => {
    session.user = LIVE_USER;
    m.me.mockResolvedValue(LIVE_USER);
    renderSection(<CategoryManager />);
    const name = await screen.findByLabelText(/New category name/);
    fireEvent.change(name, { target: { value: "Coffee" } });
    await screen.findByRole("button", { name: /^Add$/ });
    expectNoLocks();
  });
});

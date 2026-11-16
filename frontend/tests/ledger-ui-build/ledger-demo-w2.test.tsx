// M185 ledger-ui-build — PM binding decision W2 (docs/47 §4 / t_eca2953a):
//
//   "Keep POST /users/me/llm-test outside the demo exemptions. Disable Test
//    connection before dispatch for demo sessions and render exactly: 'Test
//    connection is unavailable in the demo. The demo account blocks POST
//    requests to keep the sample data read-only.' Non-demo probe behavior and
//    privacy-mode egress checks are unchanged."
//
// Plus W1's regression guard on the same surface: downloads are READS and are
// never locked or disabled (the export controls live in other cards — this
// file pins the probe, which is the one named exception).
//
// The PM also asked for the two-sided coverage: a demo action makes NO POST;
// the non-demo action still calls the typed endpoint.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LlmForm, LLM_TEST_DEMO_REASON } from "../../src/pages/Settings";
import { ToastProvider } from "../../src/components/ui/Toast";

const mocks = vi.hoisted(() => ({
  me: vi.fn(),
  testLlm: vi.fn(),
  updateMe: vi.fn(),
}));

// `me` lives in the api CLIENT (Settings imports it from there); the probe
// itself is the typed endpoint in api/users.
vi.mock("../../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../src/api/client")>();
  return { ...actual, me: mocks.me };
});
vi.mock("../../src/api/users", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../src/api/users")>();
  return { ...actual, testLlm: mocks.testLlm, updateMe: mocks.updateMe };
});

const user = (email: string) => ({
  id: 7,
  email,
  privacy_mode: false,
  currency_tracker_enabled: true,
  llm_provider: "openai",
  llm_base_url: null,
  llm_model: "gpt-4o-mini",
  encryption_required: false,
  dashboard_preset: "overview",
});

function renderForm() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <LlmForm />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

const testButton = async () => screen.findByRole("button", { name: "Test connection" });

beforeEach(() => {
  mocks.testLlm.mockResolvedValue({ ok: true, model: "gpt-4o-mini", provider: "openai" });
  mocks.updateMe.mockResolvedValue({});
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("W2 — Test connection on the demo tenant", () => {
  it("renders disabled BEFORE dispatch, with the exact local copy and no request", async () => {
    mocks.me.mockResolvedValue(user("demo@fintracker.local"));
    renderForm();

    const button = await testButton();
    expect((button as HTMLButtonElement).disabled).toBe(true);

    // Bound by aria-describedby to the reason element with its pinned testid…
    const reasonId = button.getAttribute("aria-describedby");
    expect(reasonId).toBe("llm-test-disabled-reason");
    const reason = screen.getByTestId("llm-test-disabled-reason");
    expect(reason.id).toBe("llm-test-disabled-reason");
    // …verbatim (PM W2 / docs/47 §4).
    expect(reason.textContent).toBe(LLM_TEST_DEMO_REASON);
    expect(reason.textContent).toBe(
      "Test connection is unavailable in the demo. The demo account blocks POST requests to keep the sample data read-only.",
    );

    // No padlock: this is the disabledReason pattern, not DemoLock (docs/47 §4).
    expect(screen.queryByTestId("demo-lock")).toBeNull();

    // The action makes NO POST — a click on a disabled control never fires.
    fireEvent.click(button);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mocks.testLlm).not.toHaveBeenCalled();
  });

  it("stays disabled while the probe is pending-guarded (no dispatch path)", async () => {
    mocks.me.mockResolvedValue(user("demo@fintracker.local"));
    renderForm();
    const button = await testButton();
    // No loading spinner can spin up either, because the mutation never runs.
    expect(button.getAttribute("aria-busy")).toBeNull();
    expect(mocks.testLlm).not.toHaveBeenCalled();
  });
});

describe("W2 — Test connection on a live tenant (behaviour unchanged)", () => {
  it("is enabled and calls the typed endpoint with the current config", async () => {
    mocks.me.mockResolvedValue(user("mark@example.com"));
    renderForm();

    const button = await testButton();
    expect((button as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByTestId("llm-test-disabled-reason")).toBeNull();

    fireEvent.click(button);
    await waitFor(() => expect(mocks.testLlm).toHaveBeenCalledTimes(1));
    const payload = mocks.testLlm.mock.calls[0][0] as Record<string, unknown>;
    expect(payload.llm_provider).toBe("openai");
    expect(payload.llm_model).toBe("gpt-4o-mini");
    // Nothing typed into the key field → no key goes with the probe (the
    // caller passes the field as undefined; JSON.stringify drops it on the
    // wire, which is what `api()` serialises).
    expect(payload.llm_api_key).toBeUndefined();
  });

  it("surfaces the probe result in the panel (non-demo rendering untouched)", async () => {
    mocks.me.mockResolvedValue(user("mark@example.com"));
    renderForm();
    fireEvent.click(await testButton());
    await waitFor(() =>
      expect(screen.getByText(/Connected · gpt-4o-mini/)).toBeTruthy(),
    );
  });
});

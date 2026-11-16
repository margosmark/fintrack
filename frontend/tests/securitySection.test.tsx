// M74 — Settings → Security (docs/38 "Frontend").
//
// The two flows that must never regress:
//   * enroll: setup → manual-entry secret → confirm code → the 10 recovery
//     codes rendered exactly once, with the "shown once" warning;
//   * disable: a TOTP code OR a recovery code posts to /auth/totp/disable
//     (204) and the status flips back.
// Plus the demo-tenant gate: the card renders with the same reference notice
// AccountSection uses and every control is disabled (docs/38 decision 10).
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { User } from "../src/types";

const mocks = vi.hoisted(() => ({
  restoreSession: vi.fn(),
  me: vi.fn(),
  totpStatus: vi.fn(),
  totpSetup: vi.fn(),
  totpConfirm: vi.fn(),
  totpDisable: vi.fn(),
  listPasskeys: vi.fn(),
  passkeyRegisterOptions: vi.fn(),
  passkeyRegister: vi.fn(),
  deletePasskey: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, ...mocks };
});

import { SecuritySection } from "../src/components/settings/SecuritySection";
import { ToastProvider } from "../src/components/ui/Toast";

const ALICE: User = {
  id: "user-1",
  email: "alice@example.com",
  privacy_mode: false,
  currency_tracker_enabled: false,
  llm_provider: "none",
  llm_base_url: null,
  llm_model: "",
  encryption_required: false,
};

const DEMO: User = {
  ...ALICE,
  id: "demo",
  email: "demo@fintracker.local",
};

const RECOVERY_CODES = [
  "ABCDE-FGHJK",
  "MNPQR-TVWXYZ",
  "23456-789JKM",
  "NPQRS-TVWXYZ",
  "JKMNP-RSTVW",
  "23456-78MNP",
  "QRSTV-WXYZ2",
  "34567-89JKM",
  "PQRSTV-WXYZ",
  "56789-JKMNP",
];

const PASSKEY = {
  id: 3,
  name: "YubiKey",
  created_at: "2026-09-01T10:00:00Z",
  last_used_at: null,
  transports: "internal",
};

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <SecuritySection />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.restoreSession.mockResolvedValue(null);
  mocks.me.mockResolvedValue(ALICE);
  mocks.listPasskeys.mockResolvedValue({ items: [], total: 0 });
});

describe("SecuritySection — TOTP enrollment", () => {
  it("setup → confirm shows the 10 recovery codes once", async () => {
    mocks.totpStatus.mockResolvedValue({ enabled: false, pending: false });
    mocks.totpSetup.mockResolvedValue({
      secret: "JBSWY3DPEHPK3PXP",
      otpauth_url: "otpauth://totp/fin_tracker:alice@example.com?secret=JBSWY3DPEHPK3PXP",
    });
    mocks.totpConfirm.mockResolvedValue({ recovery_codes: RECOVERY_CODES });
    // First call = pre-enrollment; after confirm, the invalidated status query
    // refetches and reports the active factor.
    mocks.totpStatus.mockResolvedValueOnce({ enabled: false, pending: false });
    mocks.totpStatus.mockResolvedValue({ enabled: true, pending: false });

    renderSection();

    // Off to begin with.
    expect(await screen.findByText(/Off — sign in currently needs only your password/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Set up two-factor authentication/ }));
    await waitFor(() => expect(mocks.totpSetup).toHaveBeenCalledTimes(1));

    // Manual entry (no camera): the secret and its otpauth URL are readable.
    const secret = (await screen.findByLabelText("Secret key")) as HTMLInputElement;
    expect(secret.value).toBe("JBSWY3DPEHPK3PXP");
    expect(screen.getByText(/otpauth:\/\/totp\/fin_tracker:alice@example\.com/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Authentication code"), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm and activate" }));

    await waitFor(() => expect(mocks.totpConfirm).toHaveBeenCalledWith("123456"));

    const panel = await screen.findByTestId("recovery-codes");
    expect(panel.textContent).toContain("shown once");
    expect(panel.querySelectorAll("li")).toHaveLength(10);
    expect(screen.getByRole("button", { name: /Copy all/ })).toBeTruthy();
    // The factor is now on, and the enrollment controls are gone.
    expect(await screen.findByText(/On — a 6-digit code/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Set up two-factor authentication/ })).toBeNull();
  });

  it("posts a recovery code to the disable endpoint", async () => {
    // On at first; the refetch after the 204 reports the factor is gone.
    mocks.totpStatus.mockResolvedValueOnce({ enabled: true, pending: false });
    mocks.totpStatus.mockResolvedValue({ enabled: false, pending: false });

    renderSection();
    expect(await screen.findByText(/On — a 6-digit code/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Turn off two-factor authentication/ }));
    const input = await screen.findByLabelText(
      "Enter a current 6-digit code or a recovery code to confirm",
    );
    fireEvent.change(input, { target: { value: "ABCDE-FGHJK" } });
    fireEvent.click(screen.getByRole("button", { name: "Turn off" }));

    await waitFor(() => expect(mocks.totpDisable).toHaveBeenCalledWith("ABCDE-FGHJK"));
    await waitFor(() => expect(mocks.totpStatus).toHaveBeenCalledTimes(2));
    // The 204 turns the factor off: the card returns to the enroll affordance.
    expect(await screen.findByRole("button", { name: /Set up two-factor/ })).toBeTruthy();
  });
});

describe("SecuritySection — passkeys", () => {
  it("lists a registered passkey with a delete affordance", async () => {
    mocks.totpStatus.mockResolvedValue({ enabled: false, pending: false });
    mocks.listPasskeys.mockResolvedValue({ items: [PASSKEY], total: 1 });

    renderSection();

    expect(await screen.findByText("YubiKey")).toBeTruthy();
    expect(screen.getByText(/Added 2026-09-01/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Remove YubiKey" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Add a passkey/ })).toBeTruthy();
    expect(mocks.passkeyRegisterOptions).not.toHaveBeenCalled();
  });
});

describe("SecuritySection — demo tenant", () => {
  it("renders the reference notice with every control disabled", async () => {
    mocks.me.mockResolvedValue(DEMO);
    mocks.totpStatus.mockResolvedValue({ enabled: false, pending: false });

    renderSection();

    expect(await screen.findByText(/disabled for the demo account/)).toBeTruthy();
    const setup = screen.getByRole("button", { name: /Set up two-factor authentication/ });
    expect((setup as HTMLButtonElement).disabled).toBe(true);
    const addPasskey = screen.getByRole("button", { name: /Add a passkey/ });
    expect((addPasskey as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.totpSetup).not.toHaveBeenCalled();
  });
});

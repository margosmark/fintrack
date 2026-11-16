// M74 — the second-factor sign-in step (docs/38 "Frontend", "Login + challenge").
//
// `POST /auth/login` now answers with a UNION: a token pair, or — for an
// account with an active TOTP factor — a `{challenge:"totp", ...}` with NO
// session. These tests pin the client-side half of that contract at the
// module boundary the app itself calls:
//
//  1. a challenge response swaps the form for the code step, the code posts
//     to /auth/login/totp, and the app is entered only after it lands;
//  2. the "Use a recovery code instead" toggle posts to /auth/login/recovery;
//  3. the SSO entry point exists only when GET /auth/oidc/config says the
//     operator enabled OIDC, and it points at the 302 the browser must follow.
//
// No live server: every client call is mocked, exactly like m68Navigation.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { User } from "../src/types";

const mocks = vi.hoisted(() => ({
  restoreSession: vi.fn(),
  login: vi.fn(),
  loginWithTotp: vi.fn(),
  loginWithRecovery: vi.fn(),
  passkeyLoginOptions: vi.fn(),
  passkeyLogin: vi.fn(),
  oidcConfig: vi.fn(),
  demoLogin: vi.fn(),
}));

// The browser half is mocked too: `navigator.credentials` does not exist in
// jsdom, and AuthContext calls `getPasskey` directly (docs/38 discoverable).
const webauthnMocks = vi.hoisted(() => ({
  createPasskey: vi.fn(),
  getPasskey: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, ...mocks };
});

vi.mock("../src/lib/webauthn", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/webauthn")>();
  return { ...actual, ...webauthnMocks };
});

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { Login } from "../src/pages/Login";

const USER: User = {
  id: "user-1",
  email: "alice@example.com",
  privacy_mode: false,
  currency_tracker_enabled: false,
  llm_provider: "none",
  llm_base_url: null,
  llm_model: "",
  encryption_required: false,
};

const PAIR = {
  access_token: "token-1",
  token_type: "bearer" as const,
  user: USER,
};

const CHALLENGE = { challenge: "totp" as const, challenge_token: "challenge-1", expires_in: 300 };

/** The passwordless start (no email): empty `allowCredentials`, UV required. */
const PASSKEY_START = {
  challenge_token: "passkey-challenge-1",
  options: {
    challenge: "a-challenge",
    rpId: "localhost",
    allowCredentials: [] as { id: string }[],
    userVerification: "required",
  },
};

/** A discoverable assertion as `getPasskey` serialises it (docs/38). */
const PASSKEY_CREDENTIAL = {
  id: "credential-1",
  rawId: "credential-1",
  type: "public-key" as const,
  response: {
    clientDataJSON: "client-data",
    authenticatorData: "auth-data",
    signature: "signature",
    userHandle: "1",
  },
};

function renderLogin() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <AuthProvider>
            <Routes>
              <Route path="/login" element={<Login />} />
              <Route path="/dashboard" element={<div data-testid="app" />} />
            </Routes>
          </AuthProvider>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** Type the credentials and press Log in — the shipped happy path. */
async function submitPassword(email: string, password: string): Promise<void> {
  fireEvent.change(await screen.findByPlaceholderText("Email"), {
    target: { value: email },
  });
  fireEvent.change(screen.getByPlaceholderText("Password"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: /^Log in$/ }));
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.restoreSession.mockResolvedValue(null);
  mocks.oidcConfig.mockResolvedValue({ enabled: false });
  mocks.demoLogin.mockResolvedValue(PAIR);
  // Passkey defaults: a start is offered, an assertion is accepted; tests
  // override whichever side they are exercising (cleared calls stay cleared).
  mocks.passkeyLoginOptions.mockResolvedValue(PASSKEY_START);
  mocks.passkeyLogin.mockResolvedValue(PAIR);
  webauthnMocks.getPasskey.mockResolvedValue(null);
});

describe("Login — TOTP challenge step", () => {
  it("switches to the code step, posts the code, then enters the app", async () => {
    mocks.login.mockResolvedValue(CHALLENGE);
    mocks.loginWithTotp.mockResolvedValue(PAIR);

    renderLogin();
    await submitPassword("alice@example.com", "pw-password");

    // The password form is replaced by the second-factor step — no session
    // exists yet, so nothing has navigated.
    const input = await screen.findByLabelText("Authentication code");
    expect(screen.queryByPlaceholderText("Password")).toBeNull();
    expect(screen.queryByTestId("app")).toBeNull();
    expect(mocks.loginWithTotp).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: /Verify and sign in/ }));

    await waitFor(() => expect(mocks.loginWithTotp).toHaveBeenCalledWith("challenge-1", "123456"));
    expect(await screen.findByTestId("app")).toBeTruthy();
  });

  it("keeps the step and shows the server's detail when the code is wrong", async () => {
    mocks.login.mockResolvedValue(CHALLENGE);
    mocks.loginWithTotp.mockRejectedValue(
      Object.assign(new Error("That code was not valid or has expired"), {
        name: "ApiError",
      }),
    );

    renderLogin();
    await submitPassword("alice@example.com", "pw-password");

    const input = await screen.findByLabelText("Authentication code");
    fireEvent.change(input, { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: /Verify and sign in/ }));

    // Still on the step, with the server's message — never a redirect.
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain(
      "That code was not valid or has expired",
    );
    expect(screen.getByLabelText("Authentication code")).toBeTruthy();
    expect(screen.queryByTestId("app")).toBeNull();
  });
});

describe("Login — recovery code toggle", () => {
  it("posts the recovery code to the recovery endpoint", async () => {
    mocks.login.mockResolvedValue(CHALLENGE);
    mocks.loginWithRecovery.mockResolvedValue(PAIR);

    renderLogin();
    await submitPassword("alice@example.com", "pw-password");
    await screen.findByLabelText("Authentication code");

    fireEvent.click(screen.getByRole("button", { name: "Use a recovery code instead" }));
    const input = await screen.findByLabelText("Recovery code");
    expect(input.getAttribute("placeholder")).toBe("XXXXX-XXXXX");

    fireEvent.change(input, { target: { value: "ABCDE-FGHJK" } });
    fireEvent.click(screen.getByRole("button", { name: /Verify and sign in/ }));

    await waitFor(() =>
      expect(mocks.loginWithRecovery).toHaveBeenCalledWith("challenge-1", "ABCDE-FGHJK"),
    );
    // The TOTP endpoint is never touched from the recovery arm.
    expect(mocks.loginWithTotp).not.toHaveBeenCalled();
    expect(await screen.findByTestId("app")).toBeTruthy();
  });
});

describe("Login — passkey entry point", () => {
  it("starts the passwordless ceremony when no email is typed", async () => {
    webauthnMocks.getPasskey.mockResolvedValue(PASSKEY_CREDENTIAL);

    renderLogin();
    const button = await screen.findByRole("button", { name: /Sign in with a passkey/ });
    // Passwordless is on the same footing as the password button: with
    // nothing typed there is still a way in (docs/38 discoverable).
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);

    await waitFor(() => expect(mocks.passkeyLoginOptions).toHaveBeenCalledWith(undefined));
    expect(webauthnMocks.getPasskey).toHaveBeenCalledWith(PASSKEY_START.options);
    await waitFor(() =>
      expect(mocks.passkeyLogin).toHaveBeenCalledWith("passkey-challenge-1", PASSKEY_CREDENTIAL),
    );
    expect(await screen.findByTestId("app")).toBeTruthy();
  });

  it("keeps the email-first ceremony when an address is typed", async () => {
    webauthnMocks.getPasskey.mockResolvedValue(PASSKEY_CREDENTIAL);

    renderLogin();
    fireEvent.change(await screen.findByPlaceholderText("Email"), {
      target: { value: "alice@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Sign in with a passkey/ }));

    await waitFor(() =>
      expect(mocks.passkeyLoginOptions).toHaveBeenCalledWith("alice@example.com"),
    );
    await waitFor(() =>
      expect(mocks.passkeyLogin).toHaveBeenCalledWith("passkey-challenge-1", PASSKEY_CREDENTIAL),
    );
    expect(await screen.findByTestId("app")).toBeTruthy();
  });

  it("names the fallbacks when the passwordless prompt yields nothing", async () => {
    renderLogin();
    fireEvent.click(await screen.findByRole("button", { name: /Sign in with a passkey/ }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Enter your email above to sign in with a passkey");
    expect(alert.textContent).toContain("use your password");
    expect(mocks.passkeyLogin).not.toHaveBeenCalled();
    expect(screen.queryByTestId("app")).toBeNull();
  });

  it("stays silent when an email-first prompt is dismissed", async () => {
    renderLogin();
    fireEvent.change(await screen.findByPlaceholderText("Email"), {
      target: { value: "alice@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Sign in with a passkey/ }));

    await waitFor(() =>
      expect(mocks.passkeyLoginOptions).toHaveBeenCalledWith("alice@example.com"),
    );
    // Dismissing the browser prompt with an address typed is not an error:
    // no message, no navigation — just back to the form (M87 contract).
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mocks.passkeyLogin).not.toHaveBeenCalled();
    expect(screen.queryByTestId("app")).toBeNull();
  });

  it("shows the server's refusal plus the fallback after a rejected assertion", async () => {
    webauthnMocks.getPasskey.mockResolvedValue(PASSKEY_CREDENTIAL);
    mocks.passkeyLogin.mockRejectedValue(
      Object.assign(new Error("Passkey authentication failed"), { name: "ApiError" }),
    );

    renderLogin();
    fireEvent.click(await screen.findByRole("button", { name: /Sign in with a passkey/ }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Passkey authentication failed");
    expect(alert.textContent).toContain("use your password");
    expect(screen.queryByTestId("app")).toBeNull();
  });
});

describe("Login — SSO entry point", () => {
  it("hides the SSO button when GET /auth/oidc/config reports enabled:false", async () => {
    mocks.oidcConfig.mockResolvedValue({ enabled: false });
    renderLogin();
    await screen.findByRole("heading", { name: "Welcome back" });
    await waitFor(() => expect(mocks.oidcConfig).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("link", { name: /Sign in with SSO/ })).toBeNull();
  });

  it("shows the SSO link pointed at the browser-followed 302 when enabled", async () => {
    mocks.oidcConfig.mockResolvedValue({ enabled: true });
    renderLogin();
    const link = await screen.findByRole("link", { name: /Sign in with SSO/ });
    expect(link.getAttribute("href")).toBe("/api/v1/auth/oidc/login");
    // A 302 must be followed by the browser, never fetched by the XHR wrapper.
    expect(link.getAttribute("href")).not.toContain("/api/v1/auth/oidc/config");
  });
});
